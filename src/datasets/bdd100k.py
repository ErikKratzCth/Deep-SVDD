from datasets.base import DataLoader
from datasets.preprocessing import center_data, normalize_data, rescale_to_unit_interval, \
    global_contrast_normalization, zca_whitening, extract_norm_and_out, learn_dictionary, pca
from utils.visualization.mosaic_plot import plot_mosaic
from utils.misc import flush_last_line
from config import Configuration as Cfg
from datasets.modules import addConvModule
import os
import numpy as np
import cPickle as pickle
from loadbdd100k import load_bdd100k_data_attribute_spec, load_bdd100k_data_filename_list


class BDD100K_DataLoader(DataLoader):

    def __init__(self):

        DataLoader.__init__(self)

        self.dataset_name = "bdd100k"

        self.n_train = Cfg.bdd100k_n_train
        self.n_val = Cfg.bdd100k_n_val
        self.n_test = Cfg.bdd100k_n_test

        self.out_frac = Cfg.bdd100k_out_frac

        self.image_height = Cfg.bdd100k_image_height
        self.image_width = Cfg.bdd100k_image_width
        self.channels = Cfg.bdd100k_channels

        self.seed = Cfg.seed

        if Cfg.ad_experiment:
            self.n_classes = 2
        else:
            self.n_classes = 6 #there are 6 different weather types, however this should not be used

        Cfg.n_batches = int(np.ceil(self.n_train * 1. / Cfg.batch_size))

        self.data_path = Cfg.bdd100k_img_folder
        self.norm_filenames = Cfg.bdd100k_norm_filenames
        self.out_filenames = Cfg.bdd100k_out_filenames

        self.label_path = Cfg.bdd100k_labels_file
        self.attributes_normal = Cfg.bdd100k_attributes_normal
        self.attributes_outlier = Cfg.bdd100k_attributes_outlier

        self.save_name_lists = Cfg.bdd100k_save_name_lists
        self.get_norm_and_out_sets = Cfg.bdd100k_get_norm_and_out_sets


        self.on_memory = True
        Cfg.store_on_gpu = True

        # load data from disk
        self.load_data()

    def check_specific(self):

        # store primal variables on RAM
        assert Cfg.store_on_gpu

    def load_data(self, original_scale=False):

        print("Loading data...")

        # load normal and outlier data
        if Cfg.bdd100k_use_file_lists:
            self._X_train, self._X_val, self._X_test, self._y_test = load_bdd100k_data_filename_list(self.data_path, self.norm_filenames, self.out_filenames, self.n_train, self.n_val, self.n_test, self.out_frac, self.image_height, self.image_width, self.channels)
        else:
            self._X_train, self._X_val, self._X_test, self._y_test = load_bdd100k_data_attribute_spec(self.data_path, self.attributes_normal, self.attributes_outlier, self.label_path, self.n_train, self.n_val, self.n_test, self.out_frac, self.image_height, self.image_width, self.channels, save_name_lists = True)

        # tranpose to channels first
        self._X_train = np.moveaxis(self._X_train,-1,1)
        self._X_val = np.moveaxis(self._X_val,-1,1)
        self._X_test = np.moveaxis(self._X_test,-1,1)


        # cast data properly
        self._X_train = self._X_train.astype(np.float32)
        self._X_val = self._X_val.astype(np.float32)
        self._X_test = self._X_test.astype(np.float32)
        self._y_test = self._y_test.astype(np.int32)

        # Train and val labels are 0, since all are normal class
        self._y_train = np.zeros((len(self._X_train),),dtype=np.int32)
        self._y_val = np.zeros((len(self._X_val),),dtype=np.int32)

        if Cfg.ad_experiment:
            # shuffle to obtain random validation splits
            np.random.seed(self.seed)

            # shuffle data (since batches are extracted block-wise)
            self.n_train = len(self._y_train)
            self.n_val = len(self._y_val)
            perm_train = np.random.permutation(self.n_train)
            perm_val = np.random.permutation(self.n_val)
            self._X_train = self._X_train[perm_train]
            self._y_train = self._y_train[perm_train]
            self._X_val = self._X_train[perm_val]
            self._y_val = self._y_train[perm_val]

            # Subset train set such that we only get batches of the same size
            assert(self.n_train >= Cfg.batch_size)
            self.n_train = (self.n_train / Cfg.batch_size) * Cfg.batch_size
            subset = np.random.choice(len(self._X_train), self.n_train, replace=False)
            self._X_train = self._X_train[subset]
            self._y_train = self._y_train[subset]

            # Adjust number of batches
            Cfg.n_batches = int(np.ceil(self.n_train * 1. / Cfg.batch_size))

        # normalize data (if original scale should not be preserved)
        if not original_scale:

            # simple rescaling to [0,1]
            normalize_data(self._X_train, self._X_val, self._X_test, scale=np.float32(255))

            # global contrast normalization
            if Cfg.gcn:
                global_contrast_normalization(self._X_train, self._X_val, self._X_test, scale=Cfg.unit_norm_used)

            # ZCA whitening
            if Cfg.zca_whitening:
                self._X_train, self._X_val, self._X_test = zca_whitening(self._X_train, self._X_val, self._X_test)

            # rescale to [0,1] (w.r.t. min and max in train data)
            rescale_to_unit_interval(self._X_train, self._X_val, self._X_test)

            # PCA
            if Cfg.pca:
                self._X_train, self._X_val, self._X_test = pca(self._X_train, self._X_val, self._X_test, 0.95)

        flush_last_line()
        print("Data loaded.")

    def build_architecture(self, nnet):
        # implementation of different network architectures
        if Cfg.bdd100k_architecture not in (1,2,3):
            # architecture spec A_B_C_D_E_F_G_H
            tmp = Cfg.bdd100k_architecture.split("_")
            use_pool = int(tmp[0]) # 1 or 0
            n_conv = int(tmp[1])
            n_dense = int(tmp[2])
            c1 = int(tmp[3])
            zsize = int(tmp[4])
            ksize= int(tmp[5])
            stride = int(tmp[6])
            pad = int(tmp[7])
            num_filters = c1

            # If using maxpool, we should have pad = same
            if use_pool:
                pad = 'same'
            else:
                pad = (pad,pad)

            if Cfg.weight_dict_init & (not nnet.pretrained):
                # initialize first layer filters by atoms of a dictionary
                W1_init = learn_dictionary(nnet.data._X_train, n_filters=8, filter_size=5, n_sample=Cfg.bdd100k_n_dict_learn)
                plot_mosaic(W1_init, title="First layer filters initialization",
                            canvas="black",
                            export_pdf=(Cfg.xp_path + "/filters_init"))
            else:
                W1_init = None

            # Build architecture

            # Add all but last conv. layer
            for i in range(n_conv-1):
                addConvModule(nnet,
                          num_filters=num_filters,
                          filter_size=(ksize,ksize),
                          W_init=W1_init,
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout,
                          p_dropout=0.2,
                          use_maxpool = use_pool,
                          stride = stride,
                          pad = pad,
                          )
                num_filters *= 2
            
            if dense_layer:
                addConvModule(nnet,
                          num_filters=num_filters,
                          filter_size=(ksize,ksize),
                          W_init=W1_init,
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout,
                          p_dropout=0.2,
                          use_maxpool = use_pool,
                          stride = stride,
                          pad = pad,
                          )

                shape_in = (None,self.image_height,W_in,num_filters)
                # Dense layer
                if Cfg.dropout:
                    nnet.addDropoutLayer()
                if Cfg.bdd100k_bias:
                    nnet.addDenseLayer(num_units=zsize)
                else:
                    nnet.addDenseLayer(num_units=zsize,
                                        b=None)
            else:
                h = self.image_height / (2**(n_conv-1))
                addConvModule(nnet,
                          num_filters=zsize,
                          filter_size=(h,h),
                          W_init=W1_init,
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=False,
                          dropout=False,
                          p_dropout=0.2,
                          use_maxpool = False,
                          stride = (1,1),
                          pad = (0,0),
                          )

        elif Cfg.bdd100k_architecture == 1: # For 256by256 images

            if Cfg.weight_dict_init & (not nnet.pretrained):
                # initialize first layer filters by atoms of a dictionary
                W1_init = learn_dictionary(nnet.data._X_train, n_filters=8, filter_size=5, n_sample=Cfg.bdd100k_n_dict_learn)
                plot_mosaic(W1_init, title="First layer filters initialization",
                            canvas="black",
                            export_pdf=(Cfg.xp_path + "/filters_init"))
            else:
                W1_init = None

            # build architecture
            nnet.addInputLayer(shape=(None, self.channels, self.image_height, self.image_width))

            # conv1 : h_in 256 -> h_out 128
            addConvModule(nnet,
                          num_filters=16 * units_multiplier,
                          filter_size=(5,5),
                          W_init=W1_init,
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout,
                          p_dropout=0.2)
            
            # conv2 : h_in 128 -> h_out 64
            addConvModule(nnet,
                          num_filters=32 * units_multiplier,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout)
            
            # conv3 : h_in 64 -> h_out 32
            addConvModule(nnet,
                          num_filters=64 * units_multiplier,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout)

            # conv4 : h_in 32 -> h_out 16
            addConvModule(nnet,
                          num_filters=64 * units_multiplier,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout)

            # conv5 : h_in 16 -> h_out 8
            addConvModule(nnet,
                          num_filters=128 * units_multiplier,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout)

            # conv6 : h_in 8 -> h_out 4
            addConvModule(nnet,
                          num_filters=256 * units_multiplier,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout)

            # Dense layer
            if Cfg.dropout:
                nnet.addDropoutLayer()
            if Cfg.bdd100k_bias:
                nnet.addDenseLayer(num_units=Cfg.bdd100k_rep_dim * units_multiplier)
            else:
                nnet.addDenseLayer(num_units=Cfg.bdd100k_rep_dim * units_multiplier,
                                   b=None)

        elif Cfg.bdd100k_architecture == 3:
            
            #(192,320) input: (2,2) maxpooling down to (3,5)-image before dense layer

            if Cfg.weight_dict_init & (not nnet.pretrained):
                # initialize first layer filters by atoms of a dictionary
                W1_init = learn_dictionary(nnet.data._X_train, n_filters=16, filter_size=5, n_sample=Cfg.bdd100k_n_dict_learn)
                plot_mosaic(W1_init, title="First layer filters initialization",
                            canvas="black",
                            export_pdf=(Cfg.xp_path + "/filters_init"))
            else:
                W1_init = None

            # build architecture 1

            # input layer
            nnet.addInputLayer(shape=(None, self.channels, self.image_height, self.image_width))

            # convlayer 1
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same')
            else:
                if Cfg.weight_dict_init & (not nnet.pretrained):
                    nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same',
                                      W=W1_init, b=None)
                else:
                    nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same',
                                      b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #pool 1
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 2
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            #pool 2
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 3
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #pool 3
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 4
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #pool4
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 5
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            # pool 5
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 6
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=256, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=256, filter_size=(5, 5),  pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # pool 6
            nnet.addMaxPool(pool_size=(2, 2))

            # dense layer 1
            if Cfg.bdd100k_bias:
                nnet.addDenseLayer(num_units=Cfg.bdd100k_rep_dim)
            else:
                nnet.addDenseLayer(num_units=Cfg.bdd100k_rep_dim, b=None)


        elif Cfg.bdd100k_architecture == 4:
            # (192,320) input: first pooling is (3,5), then (2,2) pooling down to (4,4)-image just as for CIFAR-10

            if Cfg.weight_dict_init & (not nnet.pretrained):
                # initialize first layer filters by atoms of a dictionary
                W1_init = learn_dictionary(nnet.data._X_train, n_filters=16, filter_size=5, n_sample=Cfg.bdd100k_n_dict_learn)
                plot_mosaic(W1_init, title="First layer filters initialization",
                            canvas="black",
                            export_pdf=(Cfg.xp_path + "/filters_init"))
            else:
                W1_init = None

            # build architecture 1

            # input layer
            nnet.addInputLayer(shape=(None, self.channels, self.image_height, self.image_width))

            # convlayer 1
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same')
            else:
                if Cfg.weight_dict_init & (not nnet.pretrained):
                    nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same',
                                      W=W1_init, b=None)
                else:
                    nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same',
                                      b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #pool 1
            nnet.addMaxPool(pool_size=(3, 5))

            # convlayer 2
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            #pool 2
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 3
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #pool 3
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 4
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #pool4
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 5
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            # pool 5
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 6
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=256, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=256, filter_size=(5, 5),  pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # pool 6
            nnet.addMaxPool(pool_size=(2, 2))

            # dense layer 1
            if Cfg.bdd100k_bias:
                nnet.addDenseLayer(num_units=Cfg.bdd100k_rep_dim)
            else:
                nnet.addDenseLayer(num_units=Cfg.bdd100k_rep_dim, b=None)

        else:
            raise ValueError("No valid choice of architecture")

        # Add ouput/feature layer
        if Cfg.softmax_loss:
            nnet.addDenseLayer(num_units=1)
            nnet.addSigmoidLayer()
        elif Cfg.svdd_loss:
            nnet.setFeatureLayer()  # set the currently highest layer to be the SVDD feature layer
        else:
            raise ValueError("No valid choice of loss for dataset " + self.dataset_name)

    def build_autoencoder(self, nnet):

        # implementation of different network architectures
        if Cfg.bdd100k_architecture not in (1,2,3):
            # architecture spec A_B_C_D_E_F_G_H
            tmp = Cfg.bdd100k_architecture.split("_")
            use_pool = int(tmp[0]) # 1 or 0
            n_conv = int(tmp[1])
            n_dense = int(tmp[2])
            c_out = int(tmp[3])
            zsize = int(tmp[4])
            ksize= int(tmp[5])
            stride = int(tmp[6])
            pad = int(tmp[7])
            num_filters = c1

            if use_pool:
                pad = 'same'
            else:
                pad = (pad,pad)

            if Cfg.weight_dict_init & (not nnet.pretrained):
                # initialize first layer filters by atoms of a dictionary
                W1_init = learn_dictionary(nnet.data._X_train, n_filters=8, filter_size=5, n_sample=Cfg.bdd100k_n_dict_learn)
                plot_mosaic(W1_init, title="First layer filters initialization",
                            canvas="black",
                            export_pdf=(Cfg.xp_path + "/filters_init"))
            else:
                W1_init = None

            # Build architecture

            # Add all but last conv. layer
            for i in range(n_conv-1):
                addConvModule(nnet,
                          num_filters=num_filters,
                          filter_size=(ksize,ksize),
                          W_init=W1_init,
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout,
                          p_dropout=0.2,
                          use_maxpool = use_pool,
                          stride = stride,
                          pad = pad,
                          )
                num_filters *= 2
            
            if dense_layer:
                addConvModule(nnet,
                          num_filters=num_filters,
                          filter_size=(ksize,ksize),
                          W_init=W1_init,
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout,
                          p_dropout=0.2,
                          use_maxpool = use_pool,
                          stride = stride,
                          pad = pad,
                          )

                # Dense layer
                if Cfg.dropout:
                    nnet.addDropoutLayer()
                if Cfg.bdd100k_bias:
                    nnet.addDenseLayer(num_units=zsize)
                else:
                    nnet.addDenseLayer(num_units=zsize,
                                        b=None)
            else:
                h = self.image_height / (2**(n_conv-1))
                addConvModule(nnet,
                          num_filters=zsize,
                          filter_size=(h,h),
                          W_init=W1_init,
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=False,
                          dropout=False,
                          p_dropout=0.2,
                          use_maxpool = False,
                          stride = (1,1),
                          pad = (0,0),
                          )


            nnet.setFeatureLayer()  # set the currently highest layer to be the SVDD feature layer

            # Here we use upscaling, pad should be "same"
            
            if dense_layer:
                
                h1 = self.image_height // (2**n_conv) # height = width of image going into first conv layer
                num_filters =  c_out * (2**(n_conv-1))
                nnet.addDenseLayer(num_units = h1**2 * num_filters)
                nnet.addReshapeLayer(shape=([0], num_filters, h1, h1))
                num_filters = num_filters // 2

                addConvModule(nnet,
                          num_filters=num_filters,
                          filter_size=(ksize,ksize),
                          W_init=W1_init,
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout,
                          p_dropout=0.2,
                          use_maxpool = False,
                          stride = stride,
                          pad = pad,
                          upscale = True
                          )
                num_filters //=2
            else:
                h2 = self.image_height // (2**(n_conv-1)) # height of image going in to second conv layer
                num_filters = c_out * (2**(n_conv-2))
                addConvModule(nnet,
                          num_filters=num_filters,
                          filter_size=(h2,h2),
                          W_init=W1_init,
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout,
                          p_dropout=0.2,
                          use_maxpool = False,
                          stride = (1,1),
                          pad = (0,0),
                          upscale = True
                          )
            
            # Add remaining deconv layers
            for i in range(n_conv-2):
                addConvModule(nnet,
                          num_filters=num_filters,
                          filter_size=(ksize,ksize),
                          W_init=W1_init,
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          dropout=Cfg.dropout,
                          p_dropout=0.2,
                          use_maxpool = False,
                          stride = stride,
                          pad = pad,
                          upscale = True
                          )
                num_filters //=2

        if Cfg.bdd100k_architecture == 1:
            first_layer_n_filters = 16
            if Cfg.weight_dict_init & (not nnet.pretrained):
                # initialize first layer filters by atoms of a dictionary
                W1_init = learn_dictionary(nnet.data._X_train, first_layer_n_filters, 5, n_sample=Cfg.bdd100k_n_dict_learn)
                plot_mosaic(W1_init, title="First layer filters initialization",
                            canvas="black",
                            export_pdf=(Cfg.xp_path + "/filters_init"))
            else:
                W1_init = None

            nnet.addInputLayer(shape=(None, self.channels, self.image_height, self.image_width))

            addConvModule(nnet,
                          num_filters=first_layer_n_filters,
                          filter_size=(5,5),
                          W_init=W1_init,
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm)

            addConvModule(nnet,
                          num_filters=32,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm)

            addConvModule(nnet,
                          num_filters=64,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm)

            addConvModule(nnet,
                          num_filters=64,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm)

            addConvModule(nnet,
                          num_filters=128,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm)

            addConvModule(nnet,
                          num_filters=256,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm)

            # Code Layer
            if Cfg.mnist_bias:
                nnet.addDenseLayer(num_units=Cfg.bdd100k_rep_dim)
            else:
                nnet.addDenseLayer(num_units=Cfg.bdd100k_rep_dim, b=None)
            nnet.setFeatureLayer()  # set the currently highest layer to be the SVDD feature layer
            nnet.addReshapeLayer(shape=([0], (Cfg.bdd100k_rep_dim / 16), 4, 4))
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            #nnet.addUpscale(scale_factor=(2,2))  # TODO: is this Upscale necessary? Shouldn't there be as many Upscales as MaxPools?

            # Deconv and unpool 1
            addConvModule(nnet,
                          num_filters=128,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          upscale=True)

            # Deconv and unpool 2
            addConvModule(nnet,
                          num_filters=64,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          upscale=True)

            # Deconv and unpool 3
            addConvModule(nnet,
                          num_filters=64,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          upscale=True)

            # Deconv and unpool 4
            addConvModule(nnet,
                          num_filters=64,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          upscale=True)

            # Deconv and unpool 5
            addConvModule(nnet,
                          num_filters=64,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          upscale=True)

            # Deconv and unpool 6
            addConvModule(nnet,
                          num_filters=64,
                          filter_size=(5,5),
                          bias=Cfg.bdd100k_bias,
                          pool_size=(2,2),
                          use_batch_norm=Cfg.use_batch_norm,
                          upscale=True)

            # reconstruction
            if Cfg.mnist_bias:
                nnet.addConvLayer(num_filters=self.channels,
                                  filter_size=(5, 5),
                                  pad='same')
            else:
                nnet.addConvLayer(num_filters=self.channels,
                                  filter_size=(5, 5),
                                  pad='same',
                                  b=None)
            nnet.addSigmoidLayer()


        if Cfg.bdd100k_architecture == 3:
        #(192,320) input: (2,2) maxpooling down to (3,5)-image before dense layer

            if Cfg.weight_dict_init & (not nnet.pretrained):
                # initialize first layer filters by atoms of a dictionary
                W1_init = learn_dictionary(nnet.data._X_train, n_filters=16, filter_size=5, n_sample=Cfg.bdd100k_n_dict_learn)
                plot_mosaic(W1_init, title="First layer filters initialization",
                            canvas="black",
                            export_pdf=(Cfg.xp_path + "/filters_init"))
            else:
                W1_init = None

            # build architecture 1

            # input layer
            nnet.addInputLayer(shape=(None, self.channels, self.image_height, self.image_width))

            # convlayer 1
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same')
            else:
                if Cfg.weight_dict_init & (not nnet.pretrained):
                    nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same',
                                      W=W1_init, b=None)
                else:
                    nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same',
                                      b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #pool 1
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 2
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            #pool 2
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 3
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #pool 3
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 4
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #pool4
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 5
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            # pool 5
            nnet.addMaxPool(pool_size=(2, 2))

            # convlayer 6
            if Cfg.bdd100k_bias:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=256, filter_size=(5, 5), pad='same')
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=256, filter_size=(5, 5),  pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # pool 6
            nnet.addMaxPool(pool_size=(2, 2))

            # shape is now (3,5) images with 256 channels
            # Code Layer
            nnet.addDenseLayer(num_units=Cfg.bdd100k_rep_dim, b=None)
            nnet.setFeatureLayer()  # set the currently highest layer to be the SVDD feature layer
            nnet.addReshapeLayer(shape=([0], (Cfg.bdd100k_rep_dim / (3*5)), 3, 5))
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # unpool1
            nnet.addUpscale(scale_factor=(2, 2))

            # deconv 1
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # unpool2
            nnet.addUpscale(scale_factor=(2, 2))

            # deconv2
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # unpool3    
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv3
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool4
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv4
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool5
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv5
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool6
            nnet.addUpscale(scale_factor=(2, 2))
            

            #deconv6
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=self.channels, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            nnet.addSigmoidLayer()




        if Cfg.bdd100k_architecture == 4:

            # input (256,256)
            if Cfg.weight_dict_init & (not nnet.pretrained):
                # initialize first layer filters by atoms of a dictionary
                W1_init = learn_dictionary(nnet.data._X_train, 16, 5, n_sample=Cfg.bdd100k_n_dict_learn)
                plot_mosaic(W1_init, title="First layer filters initialization",
                            canvas="black",
                            export_pdf=(Cfg.xp_path + "/filters_init"))

            #input
            nnet.addInputLayer(shape=(None, 3, self.image_height,self.image_width))

            #conv1
            if Cfg.weight_dict_init & (not nnet.pretrained):
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same',
                                  W=W1_init, b=None)
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same',
                                  b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #pool1
            nnet.addMaxPool(pool_size=(2, 2))

            #conv2
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool2
            nnet.addMaxPool(pool_size=(2, 2))

            #conv3
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool3
            nnet.addMaxPool(pool_size=(2, 2))

            #conv4
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool4
            nnet.addMaxPool(pool_size=(2, 2))

            #conv5
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool5
            nnet.addMaxPool(pool_size=(2, 2))

            #conv6
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool6
            nnet.addMaxPool(pool_size=(2, 2))

            #conv7
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=256, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool7
            nnet.addMaxPool(pool_size=(2, 2))


            # shape is now (2,2) images
            # Code Layer
            nnet.addDenseLayer(num_units=Cfg.bdd100k_rep_dim, b=None)
            nnet.setFeatureLayer()  # set the currently highest layer to be the SVDD feature layer
            nnet.addReshapeLayer(shape=([0], (Cfg.bdd100k_rep_dim / (2*2)), 2, 2))
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # unpool1
            nnet.addUpscale(scale_factor=(2, 2))

            # deconv 1
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=256, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # unpool2
            nnet.addUpscale(scale_factor=(2, 2))

            # deconv2
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # unpool3    
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv3
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool4
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv4
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool5
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv5
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool6
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv6
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool7
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv7
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool7
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv8
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=self.channels, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            nnet.addSigmoidLayer()

        if Cfg.bdd100k_architecture == 2:

            # input (256,256)
            if Cfg.weight_dict_init & (not nnet.pretrained):
                # initialize first layer filters by atoms of a dictionary
                W1_init = learn_dictionry(nnet.data._X_train, 16, 5, n_sample=500)
                plot_mosaic(W1_init, title="First layer filters initialization",
                            canvas="black",
                            export_pdf=(Cfg.xp_path + "/filters_init"))

            #input
            nnet.addInputLayer(shape=(None, 3, self.image_height,self.image_width))

            #conv1
            if Cfg.weight_dict_init & (not nnet.pretrained):
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same',
                                  W=W1_init, b=None)
            else:
                nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same',
                                  b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #pool1
            nnet.addMaxPool(pool_size=(2, 2))

            #conv2
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool2
            nnet.addMaxPool(pool_size=(2, 2))

            #conv3
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool3
            nnet.addMaxPool(pool_size=(2, 2))

            #conv4
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool4
            nnet.addMaxPool(pool_size=(2, 2))

            #conv5
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=256, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool5
            nnet.addMaxPool(pool_size=(2, 2))

            #conv6
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=512, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool6
            nnet.addMaxPool(pool_size=(2, 2))

            #conv7
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=1024, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()
            
            #pool7
            nnet.addMaxPool(pool_size=(2, 2))


            # shape is now (2,2) images
            # Code Layer
            nnet.addDenseLayer(num_units=Cfg.bdd100k_rep_dim, b=None)
            nnet.setFeatureLayer()  # set the currently highest layer to be the SVDD feature layer
            nnet.addReshapeLayer(shape=([0], (Cfg.bdd100k_rep_dim / (2*2)), 2, 2))
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # unpool1
            nnet.addUpscale(scale_factor=(2, 2))

            # deconv 1
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=1024, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # unpool2
            nnet.addUpscale(scale_factor=(2, 2))

            # deconv2
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=512, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            # unpool3    
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv3
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=256, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool4
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv4
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=128, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool5
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv5
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=64, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool6
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv6
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=32, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool7
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv7
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=16, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            #unpool7
            nnet.addUpscale(scale_factor=(2, 2))

            #deconv8
            nnet.addConvLayer(use_batch_norm=Cfg.use_batch_norm, num_filters=self.channels, filter_size=(5, 5), pad='same', b=None)
            if Cfg.leaky_relu:
                nnet.addLeakyReLU()
            else:
                nnet.addReLU()

            nnet.addSigmoidLayer()
