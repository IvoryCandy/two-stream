import numpy as np
import pickle
import time
from tqdm import tqdm
import argparse

import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
from torch.optim.lr_scheduler import ReduceLROnPlateau

import data
from misc import *
from model import *


parser = argparse.ArgumentParser(description='UCF101 spatial stream on resnet101')
parser.add_argument('--epochs', default=500, type=int, metavar='N', help='number of total epochs')
parser.add_argument('--batch-size', default=2, type=int, metavar='N', help='mini-batch size (default: 25)')
parser.add_argument('--lr', default=5e-4, type=float, metavar='LR', help='initial learning rate')
parser.add_argument('--evaluate', dest='evaluate', action='store_true', help='evaluate model on validation set')
parser.add_argument('--resume', default='./models/model.tar', type=str, metavar='PATH', help='path to latest checkpoint (default: none)')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N', help='manual epoch number (useful on restarts)')


def main():
    global arg
    arg = parser.parse_args()

    # Prepare DataLoader
    data_loader = data.SpatialDataloader(
        batch_size=arg.batch_size,
        num_workers=8,
        path='../jpegs_256/',
        ucf_list='./UCF101/UCF_list/',
        ucf_split='01',
    )

    train_loader, test_loader, test_video = data_loader.run()

    # Model
    solver = SpatialCNN(
        nb_epochs=arg.epochs,
        lr=arg.lr,
        batch_size=arg.batch_size,
        resume=arg.resume,
        start_epoch=arg.start_epoch,
        evaluate=arg.evaluate,
        train_loader=train_loader,
        test_loader=test_loader,
        test_video=test_video
    )

    # Training
    solver.build_model()
    # solver.model.load_state_dict(torch.load('./models/model.tar'))
    # torch.save(solver.model.state_dict(), './models/-1.tar')
    cudnn.benchmark = True
    solver.validate_1epoch()


class SpatialCNN:
    def __init__(self, nb_epochs, lr, batch_size, resume, start_epoch, evaluate, train_loader, test_loader, test_video):
        self.nb_epochs = nb_epochs
        self.lr = lr
        self.batch_size = batch_size
        self.resume = resume
        self.start_epoch = start_epoch
        self.evaluate = evaluate
        self.train_loader = train_loader
        self.test_loader = test_loader
        self.best_prec1 = 0
        self.test_video = test_video

        self.model = None
        self.criterion = None
        self.optimizer = None
        self.scheduler = None

    def build_model(self):
        print('==> Build model and setup loss and optimizer')
        # build model
        self.model = resnet152(pretrained=False).cuda()
        # Loss function and optimizer
        self.criterion = nn.CrossEntropyLoss().cuda()
        self.optimizer = torch.optim.SGD(self.model.parameters(), self.lr, momentum=0.9)
        self.scheduler = ReduceLROnPlateau(self.optimizer, 'min', patience=1, verbose=True)

    def resume_and_evaluate(self):
        if self.resume:
            if os.path.isfile(self.resume):
                print("==> loading checkpoint '{}'".format(self.resume))
                checkpoint = torch.load(self.resume)
                self.start_epoch = checkpoint['epoch']
                self.best_prec1 = checkpoint['best_prec1']
                self.model.load_state_dict(checkpoint['state_dict'])
                self.optimizer.load_state_dict(checkpoint['optimizer'])
                print("==> loaded checkpoint '{}' (epoch {}) (best_prec1 {})"
                      .format(self.resume, checkpoint['epoch'], self.best_prec1))
            else:
                print("==> no checkpoint found at '{}'".format(self.resume))
        if self.evaluate:
            self.epoch = 0
            prec1, val_loss = self.validate_1epoch()
            return

    def run(self):
        self.build_model()
        self.resume_and_evaluate()
        cudnn.benchmark = True

        for self.epoch in range(self.start_epoch, self.nb_epochs):
            self.train_1epoch()
            precision_1, val_loss = self.validate_1epoch()
            is_best = precision_1 > self.best_prec1
            # lr_scheduler
            self.scheduler.step(val_loss)
            # save model
            if is_best:
                self.best_prec1 = precision_1
                with open('./record/spatial/spatial_video_preds.pickle', 'wb') as f:
                    pickle.dump(self.dic_video_level_preds, f)
                f.close()

            save_checkpoint({
                'epoch': self.epoch,
                'state_dict': self.model.state_dict(),
                'best_prec1': self.best_prec1,
                'optimizer': self.optimizer.state_dict()
            }, is_best, './record/spatial/checkpoint.pth.tar', 'record/spatial/model_best.pth.tar')

    def train_1epoch(self):
        print('==> Epoch:[{0}/{1}][training stage]'.format(self.epoch, self.nb_epochs))
        batch_time = AverageMeter()
        data_time = AverageMeter()
        losses = AverageMeter()
        top1 = AverageMeter()
        top5 = AverageMeter()
        # switch to train mode
        self.model.train()
        end = time.time()
        # mini-batch training
        progress = tqdm(self.train_loader)
        for i, (data_dict, label) in enumerate(progress):

            # measure data loading time
            data_time.update(time.time() - end)

            label = label.cuda(async=True)
            target_var = Variable(label).cuda()

            # compute output
            output = Variable(torch.zeros(len(data_dict['img1']), 101).float()).cuda()
            for j in range(len(data_dict)):
                key = 'img' + str(j)
                data = data_dict[key]
                input_var = Variable(data).cuda()
                output += self.model(input_var)

            loss = self.criterion(output, target_var)

            # measure accuracy and record loss
            prec1, prec5 = accuracy(output.data, label, topk=(1, 5))
            losses.update(loss.data[0], data.size(0))
            top1.update(prec1[0], data.size(0))
            top5.update(prec5[0], data.size(0))

            # compute gradient and do SGD step
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

        info = {'Epoch': [self.epoch],
                'Batch Time': [round(batch_time.avg, 3)],
                'Data Time': [round(data_time.avg, 3)],
                'Loss': [round(losses.avg, 5)],
                'Prec@1': [round(top1.avg, 4)],
                'Prec@5': [round(top5.avg, 4)],
                'lr': self.optimizer.param_groups[0]['lr']
                }
        record_info(info, 'record/spatial/rgb_train.csv', 'train')

    def validate_1epoch(self):
        print('==> Epoch:[{0}/{1}][validation stage]'.format(0, 1))
        batch_time = AverageMeter()

        # switch to evaluate mode
        self.model.eval()
        self.dic_video_level_preds = {}
        end = time.time()
        progress = tqdm(self.test_loader)

        for i, (keys, data, label) in enumerate(progress):

            data_var = Variable(data, volatile=True).cuda(async=True)

            # compute output
            output = self.model(data_var)
            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            # Calculate video level prediction
            predictions = output.data.cpu().numpy()
            nb_data = predictions.shape[0]
            for j in range(nb_data):
                video_name = keys[j].split('/', 1)[0]

                if video_name not in self.dic_video_level_preds.keys():
                    self.dic_video_level_preds[video_name] = predictions[j, :]

                else:
                    self.dic_video_level_preds[video_name] += predictions[j, :]

        video_top1, video_top5, video_loss = self.frame2_video_level_accuracy()

        info = {'Batch Time': [batch_time],
                'Loss': [video_loss],
                'Prec@1': [video_top1],
                'Prec@5': [video_top5]}
        record_info(info, 'record/spatial/rgb_test.csv', 'test')
        return video_top1, video_loss

    def frame2_video_level_accuracy(self):

        correct = 0
        video_level_preds = np.zeros((len(self.dic_video_level_preds), 101))
        video_level_labels = np.zeros(len(self.dic_video_level_preds))
        ii = 0
        for name in sorted(self.dic_video_level_preds.keys()):

            predictions = self.dic_video_level_preds[name]
            label = int(self.test_video[name]) - 1

            video_level_preds[ii, :] = predictions
            video_level_labels[ii] = label
            ii += 1
            if np.argmax(predictions) == label:
                correct += 1

        # top1 top5
        video_level_labels = torch.from_numpy(video_level_labels).long()
        video_level_preds = torch.from_numpy(video_level_preds).float()

        top1, top5 = accuracy(video_level_preds, video_level_labels, topk=(1, 5))
        loss = self.criterion(Variable(video_level_preds).cuda(), Variable(video_level_labels).cuda())

        top1 = float(top1.numpy())
        top5 = float(top5.numpy())

        return top1, top5, loss.data.cpu().numpy()


if __name__ == '__main__':
    main()
