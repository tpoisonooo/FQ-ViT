import argparse
import os
import time
import math

from PIL import Image
import torch
import torch.nn as nn
import torchvision.transforms as transforms
import torchvision.datasets as datasets


from models import *
from config import Config

from mmcls.datasets import build_dataset as build_dataset_mmcls



parser = argparse.ArgumentParser(description="FQ-ViT")

parser.add_argument("model",
                    choices=['deit_tiny', 'deit_small', 'deit_base', 'vit_base',
                             'vit_large', 'swin_tiny', 'swin_small', 'swin_base'],
                    help="model")
parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
parser.add_argument("--quant", default=False, action="store_true")
parser.add_argument("--ptf", default=False, action="store_true")
parser.add_argument("--lis", default=False, action="store_true")
parser.add_argument("--quant-method", default="minmax",
                    choices=["minmax", "ema", "omse", "percentile"])
parser.add_argument("--calib-batchsize", default=10,
                    type=int, help="batchsize of calibration set")
parser.add_argument("--calib-iter", default=10, type=int)
parser.add_argument("--val-batchsize", default=2,
                    type=int, help="batchsize of validation set")
parser.add_argument("--num-workers", default=6, type=int,
                    help="number of data loading workers (default: 16)")
parser.add_argument("--device", default="cuda", type=str, help="device")
parser.add_argument("--print-freq", default=10,
                    type=int, help="print frequency")
parser.add_argument("--seed", default=0, type=int, help="seed")


def str2model(name):
    d = {'deit_tiny': deit_tiny_patch16_224,
         'deit_small': deit_small_patch16_224,
         'deit_base': deit_base_patch16_224,
         'vit_base': vit_base_patch16_224,
         'vit_large': vit_large_patch16_224,
         'swin_tiny': swin_tiny_patch4_window7_224,
         'swin_small': swin_small_patch4_window7_224,
         'swin_base': swin_base_patch4_window7_224,
         }
    print('Model: %s' % d[name].__name__)
    return d[name]


def seed(seed=0):
    import os
    import sys
    import torch
    import numpy as np
    import random
    sys.setrecursionlimit(100000)
    os.environ["PYTHONHASHSEED"] = str(seed)
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    np.random.seed(seed)
    random.seed(seed)


def main():
    args = parser.parse_args()
    seed(args.seed)

    device = torch.device(args.device)
    cfg = Config(args.ptf, args.lis, args.quant_method)
    model = str2model(args.model)(pretrained=True, cfg=cfg)
    model = model.to(device)

    # Note: Different models have different strategies of data preprocessing.
    model_type = args.model.split("_")[0]
    if model_type == "deit":
        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
        crop_pct = 0.875
    elif model_type == 'vit':
        mean = (0.5, 0.5, 0.5)
        std = (0.5, 0.5, 0.5)
        crop_pct = 0.9
    elif model_type == 'swin':
        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
        crop_pct = 0.9
    else:
        raise NotImplementedError

    # train_transform = build_transform(mean=mean, std=std, crop_pct=crop_pct)
    # val_transform = build_transform(mean=mean, std=std, crop_pct=crop_pct)

    # Data
    # traindir = os.path.join(args.data, 'train')
    # valdir = os.path.join(args.data, 'val')

    val_dataset = build_dataset_mmcls({'type': 'ImageNet', 
                                        'data_prefix': 'data/imagenet/test',
                                        'ann_file': 'data/imagenet/meta/test.txt', 
        'pipeline': [{'type': 'LoadImageFromFile'},
                    {'type': 'Resize', 'size': (256, -1), 'backend': 'pillow'}, 
                    {'type': 'CenterCrop', 'crop_size': 224}, 
                    {'type': 'Normalize', 'mean': [123.675, 116.28, 103.53], 'std': [58.395, 57.12, 57.375], 'to_rgb': True},
                    ]})
    # val_dataset = datasets.ImageFolder(valdir, val_transform)
    val_loader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=args.val_batchsize,
        shuffle=False,
        num_workers=args.num_workers,
        pin_memory=True,
    )
    # switch to evaluate mode
    model.eval()

    # define loss function (criterion)
    criterion = nn.CrossEntropyLoss().to(device)

    if args.quant:

        train_dataset = build_dataset_mmcls({'type': 'ImageNet', 
                                            'data_prefix': 'data/imagenet/val',
                                            'ann_file': 'data/imagenet/meta/val.txt', 
            'pipeline': [{'type': 'LoadImageFromFile'},
                        {'type': 'Resize', 'size': (256, -1), 'backend': 'pillow'}, 
                        {'type': 'CenterCrop', 'crop_size': 224}, 
                        {'type': 'Normalize', 'mean': [123.675, 116.28, 103.53], 'std': [58.395, 57.12, 57.375], 'to_rgb': True}
                        ]})
        train_loader = torch.utils.data.DataLoader(
            train_dataset,
            batch_size=args.calib_batchsize,
            shuffle=True,
            num_workers=args.num_workers,
            pin_memory=True,
            drop_last=True,
        )

        # Get calibration set.
        image_list = []
        for i, data in enumerate(train_loader):
            if i == args.calib_iter:
                break
            data = data['img'].permute(0,3,1,2).to(device)
            image_list.append(data)

        print("Calibrating...")
        model.model_open_calibrate()
        with torch.no_grad():
            for i, image in enumerate(image_list):
                if i == len(image_list)-1:
                    # This is used for OMSE method to
                    # calculate minimum quantization error
                    model.model_open_last_calibrate()
                output = model(image)
        model.model_close_calibrate()
        model.model_quant()

    print("Validating...")
    val_loss, val_prec1, val_prec5 = validate(
        args, val_loader, model, criterion, device
    )


def validate(args, val_loader, model, criterion, device):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    val_start_time = end = time.time()
    # VAL_LEN = 2000
    # for i in range(VAL_LEN):
        # data = val_loader[i]
    for i, data in enumerate(val_loader):
        target = data['gt_label'].to(device)
        data = data['img'].permute(0,3,1,2).to(device)

        with torch.no_grad():
            output = model(data)
        loss = criterion(output, target)

        # measure accuracy and record loss
        prec1, prec5 = accuracy(output.data, target, topk=(1, 5))
        losses.update(loss.data.item(), data.size(0))
        top1.update(prec1.data.item(), data.size(0))
        top5.update(prec5.data.item(), data.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print(
                "Test: [{0}/{1}]\t"
                "Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t"
                "Loss {loss.val:.4f} ({loss.avg:.4f})\t"
                "Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t"
                "Prec@5 {top5.val:.3f} ({top5.avg:.3f})".format(
                    i,
                    len(val_loader),
                    batch_time=batch_time,
                    loss=losses,
                    top1=top1,
                    top5=top5,
                )
            )
    val_end_time = time.time()
    print(" * Prec@1 {top1.avg:.3f} Prec@5 {top5.avg:.3f} Time {time:.3f}".format(
        top1=top1, top5=top5, time=val_end_time - val_start_time))

    return losses.avg, top1.avg, top5.avg


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.reshape(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].reshape(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


def build_transform(input_size=224, interpolation="bicubic",
                    mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225),
                    crop_pct=0.875):
    def _pil_interp(method):
        if method == "bicubic":
            return Image.BICUBIC
        elif method == "lanczos":
            return Image.LANCZOS
        elif method == "hamming":
            return Image.HAMMING
        else:
            return Image.BILINEAR
    resize_im = input_size > 32
    t = []
    if resize_im:
        size = int(math.floor(input_size / crop_pct))
        ip = _pil_interp(interpolation)
        t.append(
            transforms.Resize(
                size, interpolation=ip
            ),  # to maintain same ratio w.r.t. 224 images
        )
        t.append(transforms.CenterCrop(input_size))

    t.append(transforms.ToTensor())
    t.append(transforms.Normalize(mean, std))
    return transforms.Compose(t)


if __name__ == "__main__":
    main()
