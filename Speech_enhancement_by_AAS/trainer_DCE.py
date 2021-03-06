import os
from glob import glob
from itertools import chain
from shutil import copyfile

import torch
from torch import nn
from tqdm import trange
import decimal
import random

import pdb

from utils import _get_variable, _get_variable_volatile, _get_variable_nograd, AverageMeter
from decoder import GreedyDecoder
from model import *

def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.01)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.01)
        m.bias.data.fill_(0)
    elif classname.find('Embedding') != -1: # added by ken
        m.weight.data.normal_(0.0, 0.01)

class Trainer(object): # the most basic model
    def __init__(self, config, data_loader=None):
        self.config = config
        self.data_loader = data_loader  # needed for VAE

        self.lr = config.lr
        self.beta1 = config.beta1
        self.beta2 = config.beta2
        self.optimizer = config.optimizer
        self.batch_size = config.batch_size

        self.diffLoss = L1Loss_mask() # custom module

        self.valmin_iter = 0
        self.model_dir = 'logs/' + str(config.expnum)
        self.savename_G = ''
        self.decoder = GreedyDecoder(data_loader.labels)

        self.kt = 0  # used for Proportional Control Theory in BEGAN, initialized as 0
        self.lb = 0.001
        self.conv_measure = 0 # convergence measure

        self.dce_tr = AverageMeter()
        self.dce_val = AverageMeter()
        self.wer_tr = AverageMeter()
        self.cer_tr = AverageMeter()
        self.wer_val = AverageMeter()
        self.cer_val = AverageMeter()

        self.build_model()
        self.G.loss_stop = 100000
        #self.get_weight_statistic()

        if self.config.gpu >= 0:
            self.G.cuda()
            self.ASR.cuda()

        if len(self.config.load_path) > 0:
            self.load_model()

        if config.mode == 'train':
            self.logFile = open(self.model_dir + '/log.txt', 'w')

    def zero_grad_all(self):
        self.G.zero_grad()


    def build_model(self):
        print('initialize enhancement model')
        self.G = stackedBRNN(I=self.config.nFeat, H = self.config.rnn_size, L = self.config.rnn_layers, rnn_type=supported_rnns[self.config.rnn_type])

        print('load pre-trained ASR model')
        package_ASR = torch.load(self.config.ASR_path, map_location = lambda storage, loc: storage)
        self.ASR = DeepSpeech.load_model_package(package_ASR)
        # Weight initialization is done inside the module

    def load_model(self):
        print("[*] Load models from {}...".format(self.load_path))
        postfix = '_valmin'
        paths = glob(os.path.join(self.load_path, 'G{}*.pth'.format(postfix)))
        paths.sort()

        if len(paths) == 0:
            print("[!] No checkpoint found in {}...".format(self.load_path))
            assert(0), 'checkpoint not avilable'

        idxes = [int(os.path.basename(path.split('.')[0].split('_')[-1])) for path in paths]
        if self.config.start_iter <0 :
            self.config.start_iter = max(idxes)
            if(self.config.start_iter <0): # if still 0, then raise error
                raise Exception("start iter is still less than 0 --> probably try to load initial random model")

        if self.config.gpu < 0:  #CPU
            map_location = lambda storage, loc: storage
        else: # GPU
            map_location = None

        # Ver2
        print('Load models from ' + self.load_path + ', ITERATION = ' + str(self.config.start_iter))
        self.G.load_state_dict(torch.load('{}/G{}_{}.pth'.format(self.load_path[:-1], postfix, self.config.start_iter), map_location=map_location))

        print("[*] Model loaded")

    def train(self):
        # Setting
        optimizer_g = torch.optim.Adam(self.G.parameters(), lr=self.config.lr, betas=(self.beta1, self.beta2), amsgrad = True)

        for iter in trange(self.config.start_iter, self.config.max_iter):
            # Train
            data_list = self.data_loader.next(cl_ny = 'ny', type = 'train')
            inputs, cleans, mask = _get_variable_nograd(data_list[0]), _get_variable_nograd(data_list[1]), _get_variable_nograd(data_list[2])

            # forward
            outputs = self.G(inputs)
            dce, nElement = self.diffLoss(outputs, cleans, mask) # already normalized inside function

            # backward
            self.zero_grad_all()
            dce.backward()
            optimizer_g.step()

            # log
            #pdb.set_trace()
            if (iter+1) % self.config.log_iter == 0:
                str_loss= "[{}/{}] (train) DCE: {:.7f}".format(iter, self.config.max_iter, dce.data[0])
                print(str_loss)
                self.logFile.write(str_loss + '\n')
                self.logFile.flush()

            if (iter+1) % self.config.save_iter == 0:
                self.G.eval()
                # Measure performance on training subset
                self.dce_tr.reset()
                self.wer_tr.reset()
                self.cer_tr.reset()
                for _ in trange(0, len(self.data_loader.trsub_dl)):
                    data_list = self.data_loader.next(cl_ny='ny', type='trsub')
                    inputs, cleans, mask, targets, input_percentages, target_sizes = \
                        _get_variable_volatile(data_list[0]), _get_variable_volatile(data_list[1]), _get_variable_volatile(data_list[2]), \
                        data_list[3], data_list[4], data_list[5]

                    outputs = self.G(inputs)
                    dce, nElement = self.diffLoss(outputs, cleans, mask)  # already normalized inside function
                    self.dce_tr.update(dce.data[0], nElement)

                    # Greedy decodoing
                    wer, cer, nWord, nChar = self.greedy_decoding(inputs, targets, input_percentages, target_sizes)
                    self.wer_tr.update(wer, nWord)
                    self.cer_tr.update(cer, nChar)

                str_loss= "[{}/{}] (training subset) DCE: {:.7f}".format(iter, self.config.max_iter, self.dce_tr.avg)
                print(str_loss)
                self.logFile.write(str_loss + '\n')

                str_loss= "[{}/{}] (training subset) WER: {:.7f}, CER: {:.7f}".format(iter, self.config.max_iter, self.wer_tr.avg*100, self.cer_tr.avg*100)
                print(str_loss)
                self.logFile.write(str_loss + '\n')

                # Measure performance on validation data
                self.dce_val.reset()
                self.wer_val.reset()
                self.cer_val.reset()
                for _ in trange(0, len(self.data_loader.val_dl)):
                    data_list = self.data_loader.next(cl_ny='ny', type='val')
                    inputs, cleans, mask, targets, input_percentages, target_sizes = \
                        _get_variable_volatile(data_list[0]), _get_variable_volatile(data_list[1]), _get_variable_volatile(data_list[2]), \
                        data_list[3], data_list[4], data_list[5]

                    outputs = self.G(inputs)
                    dce, nElement = self.diffLoss(outputs, cleans, mask)  # already normalized inside function
                    self.dce_val.update(dce.data[0], nElement)

                    # Greedy decodoing
                    wer, cer, nWord, nChar = self.greedy_decoding(inputs, targets, input_percentages, target_sizes)
                    self.wer_val.update(wer, nWord)
                    self.cer_val.update(cer, nChar)

                str_loss= "[{}/{}] (validation) DCE: {:.7f}".format(iter, self.config.max_iter, self.dce_val.avg)
                print(str_loss)
                self.logFile.write(str_loss + '\n')

                str_loss= "[{}/{}] (validation) WER: {:.7f}, CER: {:.7f}".format(iter, self.config.max_iter, self.wer_val.avg*100, self.cer_val.avg*100)
                print(str_loss)
                self.logFile.write(str_loss + '\n')

                self.G.train() # end of validation
                self.logFile.flush()

                # Save model
                if (len(self.savename_G) > 0): # do not remove here
                    if os.path.exists(self.savename_G):
                        os.remove(self.savename_G) # remove previous model
                self.savename_G = '{}/G_{}.pth'.format(self.model_dir, iter)
                torch.save(self.G.state_dict(), self.savename_G)

                if(self.G.loss_stop > self.wer_val.avg):
                    self.G.loss_stop = self.wer_val.avg
                    savename_G_valmin_prev = '{}/G_valmin_{}.pth'.format(self.model_dir, self.valmin_iter)
                    if os.path.exists(savename_G_valmin_prev):
                        os.remove(savename_G_valmin_prev) # remove previous model

                    print('save model for this checkpoint')
                    savename_G_valmin = '{}/G_valmin_{}.pth'.format(self.model_dir, iter)
                    copyfile(self.savename_G, savename_G_valmin)
                    self.valmin_iter = iter

    def greedy_decoding(self, inputs, targets, input_percentages, target_sizes, transcript_prob=0.001):
        # unflatten targets
        split_targets = []
        offset = 0
        for size in target_sizes:
            split_targets.append(targets[offset:offset + size])
            offset += size

        # step 1) Decoding to get wer & cer
        enhanced = self.G(inputs)
        prob = self.ASR(enhanced)
        prob = prob.transpose(0,1)
        T = prob.size(0)
        sizes = input_percentages.mul_(int(T)).int()

        decoded_output, _ = self.decoder.decode(prob.data, sizes)
        target_strings = self.decoder.convert_to_strings(split_targets)
        we, ce, total_word, total_char = 0, 0, 0, 0

        for x in range(len(target_strings)):
            decoding, reference = decoded_output[x][0], target_strings[x][0]
            nChar = len(reference)
            nWord = len(reference.split())
            we_i = self.decoder.wer(decoding, reference)
            ce_i = self.decoder.cer(decoding, reference)
            we += we_i
            ce += ce_i
            total_word += nWord
            total_char += nChar
            if (random.uniform(0, 1) < transcript_prob):
                print('reference = ' + reference)
                print('decoding = ' + decoding)
                print('wer = ' + str(we_i/float(nWord)) + ', cer = ' + str(ce_i/float(nChar)))

        wer = we/total_word
        cer = ce/total_word

        return wer, cer, total_word, total_char