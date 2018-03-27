import os
import time
import argparse
import shutil

import torch
import torch.nn as nn
import torch.nn.parallel
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
from torchvision import transforms

import video_transforms
import models
import datasets

#################################################
# Global Variables
best_precision = 0
model_names = sorted(name for name in models.__dict__ if name.islower() and not name.startswith("__") and callable(models.__dict__[name]))
dataset_names = sorted(name for name in datasets.__all__)
cuda = torch.cuda.is_available()
#################################################


#################################################
# parsers
parser = argparse.ArgumentParser(description='PyTorch Two-Stream Action Recognition')
parser.add_argument('data', metavar='DIR', help='path to dataset')
parser.add_argument('--settings', metavar='DIR', default='./datasets/settings', help='path to dataset setting files')
parser.add_argument('-m', '--modality', metavar='MODALITY', default='rgb', choices=["rgb", "flow"], help='modality: rgb | flow')
parser.add_argument('-d', '--dataset', default='ucf101', choices=["ucf101", "hmdb51"], help='dataset: ucf101 | hmdb51')
parser.add_argument('-a', '--arch', metavar='ARCH', default='vgg16', choices=model_names, help='model architecture: | '.join(model_names) + ' (default: vgg16)')
parser.add_argument('-s', '--split', default=1, type=int, metavar='S', help='which split of data to work on (default: 1)')
parser.add_argument('-j', '--workers', default=8, type=int, metavar='N', help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=400, type=int, metavar='N', help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N', help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=32, type=int, metavar='N', help='mini-batch size (default: 50)')
parser.add_argument('--iter-size', default=4, type=int, metavar='I', help='iter size as in Caffe to reduce memory usage (default: 8)')
parser.add_argument('--new_length', default=1, type=int, metavar='N', help='length of sampled video frames (default: 1)')
parser.add_argument('--lr', '--learning-rate', default=0.001, type=float, metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M', help='momentum')
parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float, metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--print-freq', default=20, type=int, metavar='N', help='print frequency (default: 20)')
parser.add_argument('--save-freq', default=1, type=int, metavar='N', help='save frequency (default: 20)')
parser.add_argument('--resume', default='./checkpoints', type=str, metavar='PATH', help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true', help='evaluate model on validation set')
args = parser.parse_args()
#################################################


def main():
    global args, best_precision
    args = parser.parse_args()

    #########################################
    # create model
    print("Building model ... ")
    model = build_model()
    print("Model %s is loaded. " % (args.modality + "_" + args.arch))
    #########################################

    #########################################
    # define loss function (criterion), optimizer, scheduler
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.SGD(model.parameters(), args.lr, momentum=args.momentum, weight_decay=args.weight_decay)  # TODO: Adam?
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=150, gamma=0.1)
    #########################################

    # use cuda if run on GPU
    if cuda:
        criterion = nn.CrossEntropyLoss().cuda()
        cudnn.benchmark = True
        model.cuda()

    if not os.path.exists(args.resume):
        os.makedirs(args.resume)
    print("Saving everything to directory %s." % args.resume)

    # build training & testing dataloader
    train_loader, test_loader = build_dataloader()

    if args.evaluate:
        test(test_loader, model, criterion)
        return

    for epoch in range(args.start_epoch, args.epochs):
        scheduler.step(epoch)

        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch)

        # evaluate on validation set
        precision = test(test_loader, model, criterion)

        # remember best prec@1 and save checkpoint
        is_best = precision > best_precision
        best_precision = max(precision, best_precision)

        if (epoch + 1) % args.save_freq == 0:
            checkpoint_name = "%03d_%s" % (epoch + 1, "checkpoint.pth.tar")
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'best_precision': best_precision,
                'optimizer': optimizer.state_dict()}, is_best, checkpoint_name, args.resume)


def build_model():
    """
    build tht selected model
    :return: the model
    """

    model_name = args.modality + "_" + args.arch
    model = models.__dict__[model_name](pretrained=True, num_classes=101)
    if args.arch.startswith('vgg'):
        model.features = torch.nn.DataParallel(model.features)
    else:
        model = torch.nn.DataParallel(model)
    return model


def build_dataloader():
    """
    build the training & testing dataloader
    :return: training dataloader & testing dataloader
    """

    # Data transforming
    clip_mean = [0.485, 0.456, 0.406] * args.new_length
    clip_std = [0.229, 0.224, 0.225] * args.new_length
    transforms.Normalize(mean=clip_mean, std=clip_std)

    if args.modality == "rgb":
        scale_ratios = [1.0, 0.875, 0.75, 0.66]
    elif args.modality == "flow":
        scale_ratios = [1.0, 0.875, 0.75]
    else:
        scale_ratios = None
        print("No such modality. Only rgb and flow supported.")

    train_transform = transforms.Compose([
        transforms.Scale(256),
        video_transforms.MultiScaleCrop((224, 224), scale_ratios),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(mean=clip_mean, std=clip_std)
    ])

    val_transform = transforms.Compose([
        transforms.Scale(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(mean=clip_mean, std=clip_std)
    ])

    # data loading
    train_setting_file = "train_%s_split%d.txt" % (args.modality, args.split)
    train_split_file = os.path.join(args.settings, args.dataset, train_setting_file)
    val_setting_file = "val_%s_split%d.txt" % (args.modality, args.split)
    val_split_file = os.path.join(args.settings, args.dataset, val_setting_file)
    if not os.path.exists(train_split_file) or not os.path.exists(val_split_file):
        print("No split file exists in %s directory. Preprocess the dataset first" % args.settings)

    train_dataset = datasets.__dict__[args.dataset](data_dir=args.data,
                                                    target_dir=train_split_file,
                                                    phase="train",
                                                    modality=args.modality,
                                                    new_length=args.new_length,
                                                    video_transform=train_transform)
    val_dataset = datasets.__dict__[args.dataset](data_dir=args.data,
                                                  target_dir=val_split_file,
                                                  phase="val",
                                                  modality=args.modality,
                                                  new_length=args.new_length,
                                                  video_transform=val_transform)

    print('{} samples found, {} train samples and {} test samples.'.format(len(val_dataset) + len(train_dataset),
                                                                           len(train_dataset), len(val_dataset)))

    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=args.workers, pin_memory=True)
    test_loader = torch.utils.data.DataLoader(val_dataset, batch_size=args.batch_size, shuffle=True, num_workers=args.workers, pin_memory=True)

    return train_loader, test_loader


def train(train_loader, model, criterion, optimizer, epoch):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top3 = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()
    for i, (data, target) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        if cuda:
            data.cuda()
            target.cuda()
        data = Variable(data)
        target = Variable(target)

        output = model(data)
        loss = criterion(output, target)

        # measure accuracy and record loss
        precision_1, precision_3 = accuracy(output.data, target, top_k=(1, 3))
        losses.update(loss.data[0], data.size(0))
        top1.update(precision_1[0], data.size(0))
        top3.update(precision_3[0], data.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@3 {top3.val:.3f} ({top3.avg:.3f})'.format(epoch, i, len(train_loader), batch_time=batch_time,
                                                                  data_time=data_time, loss=losses, top1=top1,
                                                                  top3=top3))


def test(test_loader, model, criterion):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top3 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()
    for i, (data, target) in enumerate(test_loader):
        if cuda:
            data.cuda()
            target.cuda()
        data = torch.autograd.Variable(data, volatile=True)
        target = torch.autograd.Variable(target, volatile=True)

        # compute output
        output = model(data)
        loss = criterion(output, target)

        # measure accuracy and record loss
        precision_1, precision_3 = accuracy(output.data, target, top_k=(1, 3))
        losses.update(loss.data[0], data.size(0))
        top1.update(precision_1[0], data.size(0))
        top3.update(precision_3[0], data.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@3 {top3.val:.3f} ({top3.avg:.3f})'.format(i, len(test_loader), batch_time=batch_time, loss=losses, top1=top1, top3=top3))

    print(' * Prec@1 {top1.avg:.3f} Prec@3 {top3.avg:.3f}'.format(top1=top1, top3=top3))

    return top1.avg


def save_checkpoint(state, is_best, filename, resume_path):
    cur_path = os.path.join(resume_path, filename)
    best_path = os.path.join(resume_path, 'model_best.pth.tar')
    torch.save(state, cur_path)
    if is_best:
        shutil.copyfile(cur_path, best_path)


class AverageMeter(object):
    """
    Computes and stores the average and current value
    """

    def __init__(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(output, target, top_k=(1,)):
    """
    Computes the precision@k for the specified values of k
    """

    max_k = max(top_k)
    batch_size = target.size(0)

    _, prediction = output.topk(max_k, 1, True, True)
    prediction = prediction.t()
    correct = prediction.eq(target.view(1, -1).expand_as(prediction))

    res = []
    for k in top_k:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


if __name__ == '__main__':
    main()