from math import ceil
import torch

############################### My functions ###############################################

class Hook:
    """ Save input/ouput shape of each layer in the model """
    def __init__(self, module, special_param):
        self.module = module
        self.input_size = None
        self.output_size = None
        self.special_param = special_param
        # self.inputs = []#torch.empty(0, 4)
        # self.outputs= []
        
        self.active = True
        self.hook = module.register_forward_hook(self.hook_fn)
    def hook_fn(self, module, input, output):
        if not self.active:
            return
        self.input_size = input[0].shape
        self.output_size = output.shape

        # self.inputs.append(input[0])
        # self.outputs.append(output)
    def activate(self, active):
        self.active = active
    def remove(self):
        self.active = False
        self.hook.remove()
        # print("Hook is removed")

def attach_hooks_for_conv(module, name, hook, special_param=None):
    """ Attach Hook() for convolutional layers in the model """
    if hook == None:
        return
    hook[name] = Hook(module, special_param)