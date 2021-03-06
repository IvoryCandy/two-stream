from PIL import Image
import random

import torch
from torchvision import transforms
from torch.utils.data import Dataset, DataLoader
import pickle

from .split_train_test_video import *


class MotionDataset(Dataset):
    def __init__(self, dic, in_channel, root_dir, mode, transform=None):
        # Generate a 16 Frame clip
        self.keys = list(dic)
        self.values = list(dic.values())
        self.root_dir = root_dir
        self.transform = transform
        self.mode = mode
        self.in_channel = in_channel
        self.img_rows = 224
        self.img_cols = 224
        self.video = None

    def stack_optic_flow(self):
        name = 'v_' + self.video
        u = str(self.root_dir + 'u/' + name)
        v = str(self.root_dir + 'v/' + name)

        flow = torch.FloatTensor(2 * self.in_channel, self.img_rows, self.img_cols)
        i = int(self.clips_idx)

        for j in range(self.in_channel):
            idx = i + j
            idx = str(idx)
            frame_idx = 'frame' + str(idx.zfill(6))
            h_image = u + "/" + frame_idx + ".jpg"
            v_image = v + "/" + frame_idx + ".jpg"

            img_h = Image.open(h_image)
            img_v = Image.open(v_image)

            h = self.transform(img_h)
            v = self.transform(img_v)

            flow[2 * (j - 1), :, :] = h
            flow[2 * (j - 1) + 1, :, :] = v
        return flow

    def __len__(self):
        return len(self.keys)

    def __getitem__(self, idx):
        # print ('mode:',self.mode,'calling Dataset:__getitem__ @ idx=%d'%idx)

        if self.mode == 'train':
            self.video, nb_clips = self.keys[idx].split('-')
            self.clips_idx = random.randint(1, int(nb_clips))
        elif self.mode == 'val':
            self.video, self.clips_idx = self.keys[idx].split('-')
        else:
            raise ValueError('There are only train and val mode')

        label = self.values[idx]
        label = int(label) - 1
        data = self.stack_optic_flow()

        if self.mode == 'train':
            sample = (data, label)
        elif self.mode == 'val':
            sample = (self.video, data, label)
        else:
            raise ValueError('There are only train and val mode')
        return sample


class MotionDataLoader(object):
    def __init__(self, batch_size, num_workers, in_channel, path, ucf_list, ucf_split):

        self.BATCH_SIZE = batch_size
        self.num_workers = num_workers
        self.frame_count = dict()
        self.dic_test_idx = dict()
        self.dic_video_train = dict()
        self.in_channel = in_channel
        self.data_path = path
        # split the training and testing videos
        _splitter = UCF101Splitter(path=ucf_list, split=ucf_split)
        self.train_video, self.test_video = _splitter.split_video()

    def load_frame_count(self):
        with open('data/dic/frame_count.pickle', 'rb') as file:
            dic_frame = pickle.load(file)
        file.close()

        for line in dic_frame:
            video_name = line.split('_', 1)[1].split('.', 1)[0]
            n, g = video_name.split('_', 1)
            self.frame_count[video_name] = dic_frame[line]

    def run(self):
        self.load_frame_count()
        self.get_training_dic()
        self.val_sample19()

        return self.train(), self.val(), self.test_video

    def val_sample19(self):
        self.dic_test_idx = {}

        for video in self.test_video:

            sampling_interval = int((self.frame_count[video] - 10 + 1) / 19)
            for index in range(19):
                clip_idx = index * sampling_interval
                key = video + '-' + str(clip_idx + 1)
                self.dic_test_idx[key] = self.test_video[video]

    def get_training_dic(self):
        self.dic_video_train = {}
        for video in self.train_video:
            nb_clips = self.frame_count[video] - 10 + 1
            key = video + '-' + str(nb_clips)
            self.dic_video_train[key] = self.train_video[video]

    def train(self):
        training_set = MotionDataset(dic=self.dic_video_train, in_channel=self.in_channel, root_dir=self.data_path,
                                     mode='train',
                                     transform=transforms.Compose([
                                          transforms.Resize([224, 224]),
                                          transforms.ToTensor()]))

        print('==> Training data :', len(training_set), ' videos', training_set[1][0].size())

        training_loader = DataLoader(dataset=training_set, batch_size=self.BATCH_SIZE,
                                     shuffle=True, num_workers=self.num_workers, pin_memory=True)

        return training_loader

    def val(self):
        validation_set = MotionDataset(dic=self.dic_test_idx, in_channel=self.in_channel, root_dir=self.data_path,
                                       mode='val', transform=transforms.Compose([
                                            transforms.Resize([224, 224]),
                                            transforms.ToTensor()]))
        print('==> Validation data :', len(validation_set), ' frames', validation_set[1][1].size())

        test_loader = DataLoader(
            dataset=validation_set,
            batch_size=self.BATCH_SIZE,
            shuffle=False,
            num_workers=self.num_workers)

        return test_loader


if __name__ == '__main__':
    # for testing

    data_loader = MotionDataLoader(batch_size=1, num_workers=1, in_channel=10,
                                   path='../UCF101/tvl1_flow/',
                                   ucf_list='../UCF101/UCF_list/',
                                   ucf_split='01'
                                   )
    train_loader, val_loader, test_video = data_loader.run()
    print(train_loader, val_loader)
