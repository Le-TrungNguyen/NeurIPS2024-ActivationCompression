import os
import numpy as np
import torch as th
import torch.nn as nn
from pytorch_lightning import LightningModule
from torchmetrics import Accuracy
from custom_op.register import register_filter, register_SVD_filter, register_HOSVD_filter
from custom_op.conv2d.conv_avg import Conv2dAvg
from util import get_all_conv_with_name, attach_hooks_for_conv
from math import ceil
from models.encoders import get_encoder
from functools import reduce
import logging

class ClassificationModel(LightningModule):
    def __init__(self, backbone: str, backbone_args, num_classes,
                 learning_rate, weight_decay, set_bn_eval, load = None,

                 num_of_finetune=None, 
                 with_HOSVD = False, with_SVD=False, with_grad_filter=False,
                 explained_variance_threshold=None, filt_radius=None,
                 
                 use_sgd=False, momentum=0.9, anneling_steps=8008, scheduler_interval='step',
                 lr_warmup=0, init_lr_prod=0.25, 
                 setup=None):
       
        self.setup = setup
        if self.setup not in ["A", "B"]:
            raise ValueError(f"Invalid setup value: {self.setup}. It must be 'A' or 'B'.")
        
        super(ClassificationModel, self).__init__()
        self.backbone_name = backbone
        self.backbone = get_encoder(backbone, self.setup, **backbone_args)
        self.pooling = nn.AdaptiveAvgPool2d((1, 1))
        self.classifier = nn.Linear(self.backbone._out_channels[-1], num_classes)
        ##    
        self.loss = nn.CrossEntropyLoss()
        self.learning_rate = learning_rate
        self.weight_decay = weight_decay
        self.set_bn_eval = set_bn_eval
        self.acc = Accuracy(num_classes=num_classes)
        ##
        self.with_HOSVD = with_HOSVD
        self.with_SVD = with_SVD
        self.with_grad_filter = with_grad_filter
        self.with_base = not (self.with_HOSVD or self.with_SVD or self.with_grad_filter)

        self.explained_variance_threshold = explained_variance_threshold
        self.filt_radius = filt_radius
        self.use_sgd = use_sgd
        self.momentum = momentum
        self.anneling_steps = anneling_steps
        self.scheduler_interval = scheduler_interval
        self.lr_warmup = lr_warmup
        self.init_lr_prod = init_lr_prod
        self.hook = {} # Hook being a dict: where key is the module name and value is the hook
        self.num_of_finetune = num_of_finetune

        if self.with_SVD:
            self.svd_size = []


        if self.with_HOSVD:
            self.k0_hosvd = []
            self.k1_hosvd = []
            self.k2_hosvd = []
            self.k3_hosvd = []
            self.raw_size = []
            self.k_hosvd = [self.k0_hosvd, self.k1_hosvd, self.k2_hosvd, self.k3_hosvd, self.raw_size] # each element of this list is a list of length equal to (num_train_batches * num_of_finetune), since each batch will have a different k.
            
        ###################################### Create configuration to modify model #########################################
        all_conv_layers = get_all_conv_with_name(self) # A dictionary contains all conv2d layers (value) and their names (key)
        self.filter_cfgs = {}

        if num_of_finetune == "all": # If finetune all conv2d layers
            self.num_of_finetune = len(all_conv_layers)
            if with_SVD:
                self.filter_cfgs = {"explained_variance_threshold": explained_variance_threshold, "svd_size": None}
            elif with_HOSVD:
                self.filter_cfgs = {"explained_variance_threshold": explained_variance_threshold, "k_hosvd": None}
            elif with_grad_filter:
                self.filter_cfgs = {"radius": filt_radius}

            self.filter_cfgs["finetuned_layer"] = all_conv_layers
            self.filter_cfgs["type"] = "conv"

        elif num_of_finetune > len(all_conv_layers): # If finetune all conv2d layers
            self.num_of_finetune = len(all_conv_layers)
            logging.info("[Warning] number of finetuned layers is bigger than the total number of conv layers in the network => Finetune all the network")
            if with_SVD:
                self.filter_cfgs = {"explained_variance_threshold": explained_variance_threshold, "svd_size": None}
            elif with_HOSVD:
                self.filter_cfgs = {"explained_variance_threshold": explained_variance_threshold, "k_hosvd": None}
            elif with_grad_filter:
                self.filter_cfgs = {"radius": filt_radius}

            self.filter_cfgs["finetuned_layer"] = all_conv_layers
            self.filter_cfgs["type"] = "conv"

        elif num_of_finetune is not None and num_of_finetune != 0 and num_of_finetune != "all":
            all_conv_layers = dict(list(all_conv_layers.items())[-num_of_finetune:]) # Only apply filter for num_of_finetune last conv2d layers
            for name, mod in self.named_modules():
                if len(list(mod.children())) == 0 and name not in all_conv_layers.keys() and name != '':
                    mod.eval()
                    for param in mod.parameters():
                        param.requires_grad = False # Freeze layer
                elif name in all_conv_layers.keys():
                    break
            if with_SVD:
                self.filter_cfgs = {"explained_variance_threshold": explained_variance_threshold, "svd_size": None}
            elif with_HOSVD:
                self.filter_cfgs = {"explained_variance_threshold": explained_variance_threshold, "k_hosvd": None}
            elif with_grad_filter:
                self.filter_cfgs = {"radius": filt_radius}
            self.filter_cfgs["finetuned_layer"] = all_conv_layers
            self.filter_cfgs["type"] = "conv"
        
        elif num_of_finetune == 0 or num_of_finetune == None: # If no finetune => freeze all
            logging.info("[Warning] number of finetuned layers is 0 => Freeze all layers !!")
            for name, mod in self.named_modules():
                if name != '':
                    path_seq = name.split('.')
                    target = reduce(getattr, path_seq, self)
                    target.eval()
                    for param in target.parameters():
                        param.requires_grad = False # Freeze layer
            self.filter_cfgs = -1
        else:
            logging.info("[Warning] Missing configuration !!")
            self.filter_cfgs = -1
        ###########################################################################################################
        

        if load != None:
            state_dict = th.load(load)['state_dict']
            self.load_state_dict(state_dict)
        if self.with_HOSVD:
            register_HOSVD_filter(self, self.filter_cfgs)
        elif self.with_SVD:
            register_SVD_filter(self, self.filter_cfgs)
        elif self.with_grad_filter:
            register_filter(self, self.filter_cfgs)

        self.acc.reset()
    def activate_hooks(self, is_activated=True):
        for h in self.hook:
            self.hook[h].activate(is_activated)

    def remove_hooks(self):
        for h in self.hook:
            self.hook[h].remove()
        logging.info("Hook is removed")

    def reset_svd_size(self):
        self.svd_size.clear() 

    def reset_k_hosvd(self):
        self.k0_hosvd.clear()
        self.k1_hosvd.clear()
        self.k2_hosvd.clear()
        self.k3_hosvd.clear()
        self.raw_size.clear()

    def attach_memory_info_list_HOSVD_SVD(self):
        for name in self.filter_cfgs["finetuned_layer"]:
            path_seq = name.split('.')
            target = reduce(getattr, path_seq, self)
            for param in target.parameters():
                param.requires_grad = False
            
        if self.with_SVD:
            self.filter_cfgs["svd_size"] = self.svd_size
            register_SVD_filter(self, self.filter_cfgs)

        elif self.with_HOSVD:
            self.filter_cfgs["k_hosvd"] = self.k_hosvd
            register_HOSVD_filter(self, self.filter_cfgs)
        self.update_optimizer()


    def get_activation_size(self, data, consider_active_only=True, element_size=4, unit="MB"): # For VanillaBP and Gradient Filter
        # Register hook to log input/output size
        attach_hooks_for_conv(self, consider_active_only=consider_active_only)
        self.activate_hooks(True)
        #############################################################################
        _, first_hook = next(iter(self.hook.items()))
        if first_hook.active: logging.info("Hook is activated")
        else: logging.info("[Warning] Hook is not activated !!")
        #############################################################################
        # Feed one sample of data into model to record activation size
        if isinstance(first_hook.module, Conv2dAvg) or isinstance(first_hook.module, nn.modules.conv.Conv2d):
            _ = self(data)

        num_element = 0
        for name in self.hook:
            input_size = self.hook[name].input_size
            if isinstance(self.hook[name].module, Conv2dAvg):
                stride = self.hook[name].module.stride
                x_h, x_w = input_size[-2:]
                h, w = self.hook[name].output_size[-2:] 

                p_h, p_w = ceil(h / self.filt_radius), ceil(w / self.filt_radius)
                x_order_h, x_order_w = self.filt_radius * stride[0], self.filt_radius * stride[1]
                x_pad_h, x_pad_w = ceil((p_h * x_order_h - x_h) / 2), ceil((p_w * x_order_w - x_w) / 2)

                x_sum_height = ((x_h + 2 * x_pad_h - x_order_h) // x_order_h) + 1
                x_sum_width = ((x_w + 2 * x_pad_w - x_order_w) // x_order_w) + 1

                num_element += int(input_size[0] * input_size[1] * x_sum_height * x_sum_width)
            
            elif isinstance(self.hook[name].module, nn.modules.conv.Conv2d):
                num_element += int(input_size[0] * input_size[1] * input_size[2] * input_size[3])

        self.remove_hooks()

        if unit == "Byte":
            res = str(num_element*element_size)
        if unit == "MB":
            res = str((num_element*element_size)/(1024*1024))
        elif unit == "KB":
            res = str((num_element*element_size)/(1024))
        else:
            raise ValueError("Unit is not suitable")
        
        with open(os.path.join(self.logger.log_dir, f'activation_memory_{unit}.log'), "a") as file:
            file.write(f"Activation memory is {res} {unit}\n")
    
    def get_activation_size_svd(self, num_batches, element_size=4, unit="MB"):
        # device = th.device("cuda" if th.cuda.is_available() else "cpu")
        svd_size_tensor= th.stack(self.svd_size).t().float() # Shape: (3 shapes of components, #batches * num_of_finetune)
        svd_size_tensor = svd_size_tensor.view(3, num_batches, self.num_of_finetune) # Shape: (3 shapes of components, #batches, num_of_finetune)
        svd_size_tensor = svd_size_tensor.permute(2, 1, 0) # Shape: (num_of_finetune, #batches, 3 shapes of components)

        # Average of each finetuned layers (along #batch)
        num_element_all = th.mean(svd_size_tensor[:, :, 0] * svd_size_tensor[:, :, 1] + svd_size_tensor[:, :, 1] * svd_size_tensor[:, :, 2], dim=1)
        # Sum of average of all finetuned layers
        num_element = th.sum(num_element_all)

        if unit == "Byte":
            res = num_element*element_size
        elif unit == "MB":
            res = (num_element*element_size)/(1024*1024)
        elif unit == "KB":
            res = (num_element*element_size)/(1024)
        else:
            raise ValueError("Unit is not suitable")
        
        with open(os.path.join(self.logger.log_dir, f'activation_memory_{unit}.log'), "a") as file:
            file.write(str(self.current_epoch) + "\t" + str(float(res)) + "\n")
    

    def get_activation_size_hosvd(self, num_batches, element_size=4, unit="MB"):
        device = th.device("cuda" if th.cuda.is_available() else "cpu")

        k_hosvd_tensor = th.tensor(self.k_hosvd[:4], device=device).float() # Shape: (4 k, #batches * num_of_finetune)
        k_hosvd_tensor = k_hosvd_tensor.view(4, num_batches, self.num_of_finetune) # Shape: (4 k, #batches, num_of_finetune)
        k_hosvd_tensor = k_hosvd_tensor.permute(2, 1, 0) # Shape: (num_of_finetune, #batch, 4 k)
        

        raw_shapes = th.tensor(self.k_hosvd[4], device=device).reshape(num_batches, self.num_of_finetune, 4) # Shape: (#batch, num_of_finetune, 4 shapes)
        raw_shapes = raw_shapes.permute(1, 0, 2) # Shape: (num_of_finetune, #batch, 4 shapes)

        '''
        Iterate through each layer (dimension 1: num_of_finetune) 
        -> Iterate through each batch (dimension 2: #batches), calculate the number of elements here, then infer the average number of elements per batch for each layer 
        -> Sum everything to get the average number of elements per batch across all layers.
        '''
        num_element_all = th.sum(
            k_hosvd_tensor[:, :, 0] * k_hosvd_tensor[:, :, 1] * k_hosvd_tensor[:, :, 2] * k_hosvd_tensor[:, :, 3]
            + k_hosvd_tensor[:, :, 0] * raw_shapes[:, :, 0]
            + k_hosvd_tensor[:, :, 1] * raw_shapes[:, :, 1]
            + k_hosvd_tensor[:, :, 2] * raw_shapes[:, :, 2]
            + k_hosvd_tensor[:, :, 3] * raw_shapes[:, :, 3],
            dim=1
        )
        num_element = th.sum(num_element_all) / k_hosvd_tensor.shape[1]

        if unit == "Byte":
            res = num_element*element_size
        elif unit == "MB":
            res = (num_element*element_size)/(1024*1024)
        elif unit == "KB":
            res = (num_element*element_size)/(1024)
        else:
            raise ValueError("Unit is not suitable")
        
        with open(os.path.join(self.logger.log_dir, f'activation_memory_{unit}.log'), "a") as file:
            file.write(str(self.current_epoch) + "\t" + str(float(res)) + "\n")

    def configure_optimizers(self):
        if self.use_sgd:
            optimizer = th.optim.SGD(filter(lambda p: p.requires_grad, self.parameters()),
                                     lr=self.learning_rate, weight_decay=self.weight_decay, momentum=self.momentum)
            if self.lr_warmup == 0:
                scheduler = th.optim.lr_scheduler.CosineAnnealingLR(
                    optimizer, self.anneling_steps, eta_min=0.1 * self.learning_rate)
            else:
                def _lr_fn(epoch):
                    if epoch < self.lr_warmup:
                        lr = self.init_lr_prod + (1 - self.init_lr_prod) / (self.lr_warmup - 1) * epoch
                    else:
                        e = epoch - self.lr_warmup
                        es = self.anneling_steps - self.lr_warmup
                        lr = 0.5 * (1 + np.cos(np.pi * e / es))
                    return lr
                scheduler = th.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=_lr_fn)
            sch = {
                "scheduler": scheduler,
                'interval': self.scheduler_interval,
                'frequency': 1
            }
            return [optimizer], [sch]
        optimizer = th.optim.Adam(filter(lambda p: p.requires_grad, self.parameters()),
                                  lr=self.learning_rate, weight_decay=self.weight_decay, betas=(0.8, 0.9))
        return [optimizer]

    def update_optimizer(self):
        optimizer = self.trainer.optimizers[0]

        for name in self.filter_cfgs["finetuned_layer"]:
            path_seq = name.split('.')
            target = reduce(getattr, path_seq, self) # Turn on gradient

            optimizer.add_param_group({
                'params': filter(lambda p: p.requires_grad, target.parameters())
            })

    def bn_eval(self):
        def f(m):
            classname = m.__class__.__name__
            if classname.find('BatchNorm') != -1:
                m.eval()
            m.momentum = 1.0
        self.apply(f)

    def forward(self, x):
        feat = self.backbone(x)[-1]
        feat = self.pooling(feat)
        feat = feat.flatten(start_dim=1)
        logit = self.classifier(feat)
        return logit

    def training_step(self, train_batch, batch_idx):
        if self.set_bn_eval:
            self.bn_eval()
        img, label = train_batch['image'], train_batch['label']
        if img.shape[1] == 1:
            img = th.cat([img] * 3, dim=1)
        logits = self.forward(img)
        pred_cls = th.argmax(logits, dim=-1)
        acc = th.sum(pred_cls == label) / label.shape[0]
        loss = self.loss(logits, label)
        self.log("Train/Loss", loss)
        self.log("Train/Acc", acc)
        return {'loss': loss, 'acc': acc}

    def training_epoch_end(self, outputs):
        with open(os.path.join(self.logger.log_dir, 'train_loss.log'), 'a') as f:
            mean_loss = th.stack([o['loss'] for o in outputs]).mean()
            f.write(f"{self.current_epoch} {mean_loss}")
            f.write("\n")

        with open(os.path.join(self.logger.log_dir, 'train_acc.log'), 'a') as f:
            mean_acc = th.stack([o['acc'] for o in outputs]).mean()
            f.write(f"{self.current_epoch} {mean_acc}")
            f.write("\n")

    def validation_step(self, val_batch, batch_idx):
        img, label = val_batch['image'], val_batch['label']
        if img.shape[1] == 1:
            img = th.cat([img] * 3, dim=1)
        logits = self.forward(img)
        probs = logits.softmax(dim=-1)
        pred = th.argmax(logits, dim=1)
        self.acc(probs, label)
        loss = self.loss(logits, label)
        self.log("Val/Loss", loss)
        return {'pred': pred, 'prob': probs, 'label': label}

    def validation_epoch_end(self, outputs):
        f = open(os.path.join(self.logger.log_dir, 'val.log'), 'a') if self.logger is not None else None
        acc = self.acc.compute()
        if self.logger is not None:
            f.write(f"{self.current_epoch} {acc}\n")
            f.close()
        self.log("Val/Acc", acc)
        self.log("val-acc", acc)
        self.acc.reset()
