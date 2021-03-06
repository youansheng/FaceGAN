import numpy as np
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data

import os
from collections import OrderedDict
from torch.autograd import Variable
import util.util as util
from util.image_pool import ImagePool
from .base_model import BaseModel
from . import networks


class Pts2Face_Model(BaseModel):
    def name(self):
        return 'Pts2Face_Model'

    def initialize(self, opt):
        BaseModel.initialize(self, opt)
        if self.isTrain:
            self.epoch_F = opt.epoch_F
        self.normalize = opt.input_normalize
        self.isTrain = opt.isTrain
        # define tensors
        self.input_A_Pts = self.Tensor(opt.batchSize, opt.input_nc + opt.num_pts,
                                   opt.fineSize, opt.fineSize)
        self.input_B = self.Tensor(opt.batchSize, opt.output_nc,
                                   opt.fineSize, opt.fineSize)
        self.input_A = self.Tensor(opt.batchSize, opt.input_nc,
                                   opt.fineSize, opt.fineSize)

        # load/define networks
        self.netG = networks.define_G(opt.input_nc + opt.num_pts, opt.output_nc, opt.ngf,
                                      opt.which_model_netG, opt.norm, opt.use_dropout, self.gpu_ids)
        self.netF = networks.define_F(opt.num_classes, self.gpu_ids)

        if os.path.isfile(opt.F_weights):
            print("=> loading pretrained F model '{}'".format(opt.F_weights))
            checkpoint = torch.load(opt.F_weights)
            self.load_lightcnn_state_dict(self.netF, checkpoint['state_dict'])
            print('\n=> loaded pretrained F model from {}'.format(opt.F_weights))
        else:
            print("\n=> no pretrained F model found at '{}'".format(opt.F_weights))

        if self.isTrain:
            use_sigmoid = opt.no_lsgan
            self.netD = networks.define_D(opt.input_nc + opt.num_pts + opt.output_nc, opt.ndf,
                                          opt.which_model_netD,
                                          opt.n_layers_D, opt.norm, use_sigmoid, self.gpu_ids)

        print('---------- Networks initialized -------------')
        networks.print_network(self.netG)
        networks.print_network(self.netF)
        if self.isTrain:
            networks.print_network(self.netD)
        print('-----------------------------------------------')

        if not self.isTrain or opt.continue_train:
            print('---------- Loading netG...')
            self.load_network(self.netG, 'G', opt.which_epoch)
            print('---------- Loading netG success.')
            if self.isTrain:
                print('---------- Loading netD...')
                self.load_network(self.netD, 'D', opt.which_epoch)
                print('---------- Loading netD success.')

        if self.isTrain:
            self.fake_AB_pool = ImagePool(opt.pool_size)
            self.old_lr = opt.lr
            # define loss functions
            self.criterionGAN = networks.GANLoss(use_lsgan=not opt.no_lsgan, tensor=self.Tensor)
            self.criterionL1 = torch.nn.L1Loss()

            # initialize optimizers
            self.optimizer_G = torch.optim.Adam(self.netG.parameters(),
                                                lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizer_D = torch.optim.Adam(self.netD.parameters(),
                                                lr=opt.lr, betas=(opt.beta1, 0.999))



    def set_input(self, input):
        input_A_Pts = input['A_Pts']
        self.input_A_Pts.resize_(input_A_Pts.size()).copy_(input_A_Pts)
        if self.isTrain:
            input_A = input['A']
            self.input_A.resize_(input_A.size()).copy_(input_A)
            input_B = input['B']
            self.input_B.resize_(input_B.size()).copy_(input_B)
        self.B_Path = input['B_path']

    def forward(self):
        self.real_A = Variable(self.input_A)
        self.real_A_Pts = Variable(self.input_A_Pts)
        self.fake_B = self.netG.forward(self.real_A_Pts)
        self.real_B = Variable(self.input_B)
        

    # no backprop gradients
    def test(self):
        self.real_A_Pts = Variable(self.input_A_Pts, volatile=True)
        self.fake_B = self.netG.forward(self.real_A_Pts)


    # get image paths
    def get_image_paths(self):
        return self.B_Path

    def backward_D(self):
        # Fake
        # stop backprop to the generator by detaching fake_B
        fake_AB = self.fake_AB_pool.query(torch.cat((self.real_A_Pts, self.fake_B), 1))
        self.pred_fake = self.netD.forward(fake_AB.detach())
        self.loss_D_fake = self.criterionGAN(self.pred_fake, False)

        # Real
        real_AB = torch.cat((self.real_A_Pts, self.real_B), 1)
        self.pred_real = self.netD.forward(real_AB)
        self.loss_D_real = self.criterionGAN(self.pred_real, True)

        # Combined loss
        self.loss_D = (self.loss_D_fake + self.loss_D_real) * 0.5

        self.loss_D.backward()

    def backward_G(self,epoch):
        # First, G(A) should fake the discriminator
        fake_AB = torch.cat((self.real_A_Pts, self.fake_B), 1)
        pred_fake = self.netD.forward(fake_AB)
        self.loss_G_GAN = self.criterionGAN(pred_fake, True)

        # Second, G(A) = B
        self.loss_G_L1 = self.criterionL1(self.fake_B, self.real_B) * self.opt.lambda_A
        if epoch > self.epoch_F:

            # Third, F(G(A)) = F(A), F(G(A))=F(B)
            if self.opt.input_nc != 1:
                self.gray_real_A = self.real_A.mean(1, keepdim=True)
            if self.opt.output_nc != 1:
                self.gray_fake_B = self.fake_B.mean(1, keepdim=True)
                self.gray_real_B = self.real_B.mean(1, keepdim=True)
            else:
                self.gray_fake_B = self.fake_B
                self.gray_real_B = self.real_B
            t, self.feat_real_A = self.netF.forward(self.gray_real_A)
            t, self.feat_fake_B = self.netF.forward(self.gray_fake_B)
            t, self.feat_real_B = self.netF.forward(self.gray_real_B)

            self.loss_F_L1_B = self.criterionL1(self.feat_fake_B, self.feat_real_B.detach()) * self.opt.lambda_F
            self.loss_F_L1_A = self.criterionL1(self.feat_fake_B, self.feat_real_A.detach()) * self.opt.lambda_F

            self.loss_G = self.loss_G_GAN + self.loss_G_L1 + self.loss_F_L1_A + self.loss_F_L1_B
        else:
            self.loss_G = self.loss_G_GAN + self.loss_G_L1

        self.loss_G.backward()

    def optimize_parameters(self,epoch):
        self.forward()

        self.optimizer_D.zero_grad()
        self.backward_D()
        self.optimizer_D.step()

        self.optimizer_G.zero_grad()
        self.backward_G(epoch)
        self.optimizer_G.step()

    def get_current_errors(self,epoch):
        if epoch> self.epoch_F:
            return OrderedDict([('G_GAN', self.loss_G_GAN.data[0]),
                                ('G_L1', self.loss_G_L1.data[0]),
                                ('D_real', self.loss_D_real.data[0]),
                                ('D_fake', self.loss_D_fake.data[0]),
                                ('F_L1_A', self.loss_F_L1_A.data[0]),
                                ('F_L1_B', self.loss_F_L1_B.data[0])
                                ])
        else:
            return OrderedDict([('G_GAN', self.loss_G_GAN.data[0]),
                                ('G_L1', self.loss_G_L1.data[0]),
                                ('D_real', self.loss_D_real.data[0]),
                                ('D_fake', self.loss_D_fake.data[0])
                                ])


    def get_current_visuals(self):
        real_A = util.tensor2im(self.real_A.data,normalize=self.normalize)
        fake_B = util.tensor2im(self.fake_B.data,normalize=self.normalize)
        real_B = util.tensor2im(self.real_B.data,normalize=self.normalize)
        return OrderedDict([('real_A', real_A), ('fake_B', fake_B), ('real_B', real_B)])

    def get_test_visuals(self):
        fake_B = util.tensor2im(self.fake_B.data,normalize=self.normalize)
        return OrderedDict([('fake_B', fake_B)])

    def save(self, label):
        self.save_network(self.netG, 'G', label, self.gpu_ids)
        self.save_network(self.netD, 'D', label, self.gpu_ids)

    def update_learning_rate(self):
        lrd = self.opt.lr / self.opt.niter_decay
        lr = self.old_lr - lrd
        for param_group in self.optimizer_D.param_groups:
            param_group['lr'] = lr
        for param_group in self.optimizer_G.param_groups:
            param_group['lr'] = lr
        print('update learning rate: %f -> %f' % (self.old_lr, lr))
        self.old_lr = lr
