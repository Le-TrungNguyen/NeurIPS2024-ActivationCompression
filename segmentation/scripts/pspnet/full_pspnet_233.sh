python train.py configs/pspnet/full_pspnet_r18-d8_512x512_20k_voc12aug.py --load-from calib/calib_pspnet_r18-d8_512x512_1k_voc12aug_cityscapes/version_0/latest.pth --cfg-options data.samples_per_gpu=8 --seed 233