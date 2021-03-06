#-------------------------------------------------------------------------------
# Name:        run_joint_pretrain.py
# Purpose:     Pretrain regression module and attation module for joint prediction
# RigNet Copyright 2020 University of Massachusetts
# RigNet is made available under General Public License Version 3 (GPLv3), or under a Commercial License.
# Please see the LICENSE README.txt file in the main directory for more information and instruction on using and licensing RigNet.
#-------------------------------------------------------------------------------

import sys
sys.path.append("./")
import os
import shutil
import argparse
import numpy as np

from utils.log_utils import AverageMeter
from utils.os_utils import isdir, mkdir_p, isfile
from utils.io_utils import output_point_cloud_ply

import torch
import torch.backends.cudnn as cudnn
from torch_geometric.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from datasets.skeleton_dataset import GraphDataset
from models.GCN import JointPredNet
from models.supplemental_layers.pytorch_chamfer_dist import chamfer_distance_with_average

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def save_checkpoint(state, is_best, checkpoint='checkpoint', filename='checkpoint.pth.tar', snapshot=None):
    filepath = os.path.join(checkpoint, filename)
    torch.save(state, filepath)

    if snapshot and state['epoch'] % snapshot == 0:
        shutil.copyfile(filepath, os.path.join(checkpoint, 'checkpoint_{}.pth.tar'.format(state['epoch'])))

    if is_best:
        shutil.copyfile(filepath, os.path.join(checkpoint, 'model_best.pth.tar'))


def main(args):
    global device
    lowest_loss = 1e20

    # create checkpoint dir and log dir
    if not isdir(args.checkpoint):
        print("Create new checkpoint folder " + args.checkpoint)
    mkdir_p(args.checkpoint)
    if not args.resume:
        if isdir(args.logdir):
            shutil.rmtree(args.logdir)
        mkdir_p(args.logdir)

    # create model
    if args.arch == 'jointnet':
        model = JointPredNet(out_channels=3, input_normal=args.input_normal, arch=args.arch, aggr=args.aggr)
    elif args.arch == 'masknet':
        model = JointPredNet(out_channels=1, input_normal=args.input_normal, arch=args.arch, aggr=args.aggr)

    model.to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    if args.resume:
        if isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            lowest_loss = checkpoint['lowest_loss']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True
    print('    Total params: %.2fM' % (sum(p.numel() for p in model.parameters()) / 1000000.0))
    train_loader = DataLoader(GraphDataset(root=args.train_folder), batch_size=args.train_batch, shuffle=True, follow_batch=['joints'])
    val_loader = DataLoader(GraphDataset(root=args.val_folder), batch_size=args.test_batch, shuffle=False, follow_batch=['joints'])
    test_loader = DataLoader(GraphDataset(root=args.test_folder), batch_size=args.test_batch, shuffle=False, follow_batch=['joints'])
    if args.evaluate:
        print('\nEvaluation only')
        test_loss = test(test_loader, model, args, save_result=True, best_epoch=args.start_epoch)
        print('test_loss {:8f}'.format(test_loss))
        return

    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, args.schedule, gamma=args.gamma)
    logger = SummaryWriter(log_dir=args.logdir)
    for epoch in range(args.start_epoch, args.epochs):
        lr = scheduler.get_last_lr()
        print('\nEpoch: %d | LR: %.8f' % (epoch + 1, lr[0]))
        train_loss = train(train_loader, model, optimizer, args)
        val_loss = test(val_loader, model, args)
        test_loss = test(test_loader, model, args)
        scheduler.step()
        print('Epoch{:d}. train_loss: {:.6f}.'.format(epoch + 1, train_loss))
        print('Epoch{:d}. val_loss: {:.6f}.'.format(epoch + 1, val_loss))
        print('Epoch{:d}. test_loss: {:.6f}.'.format(epoch + 1, test_loss))
        # remember best acc and save checkpoint
        is_best = val_loss < lowest_loss
        lowest_loss = min(val_loss, lowest_loss)
        save_checkpoint({'epoch': epoch + 1, 'state_dict': model.state_dict(), 'lowest_loss': lowest_loss, 'optimizer': optimizer.state_dict()},
                        is_best, checkpoint=args.checkpoint)

        info = {'train_loss': train_loss, 'val_loss': val_loss, 'test_loss': test_loss}
        for tag, value in info.items():
            logger.add_scalar(tag, value, epoch+1)

    print("=> loading checkpoint '{}'".format(os.path.join(args.checkpoint, 'model_best.pth.tar')))
    checkpoint = torch.load(os.path.join(args.checkpoint, 'model_best.pth.tar'))
    best_epoch = checkpoint['epoch']
    model.load_state_dict(checkpoint['state_dict'])
    print("=> loaded checkpoint '{}' (epoch {})".format(os.path.join(args.checkpoint, 'model_best.pth.tar'), best_epoch))
    test_loss = test(test_loader, model, args, save_result=True, best_epoch=best_epoch)
    print('Best epoch:\n test_loss {:8f}'.format(test_loss))


def train(train_loader, model, optimizer, args):
    global device
    model.train()  # switch to train mode
    loss_meter = AverageMeter()
    for data in train_loader:
        data = data.to(device)
        optimizer.zero_grad()
        if args.arch == 'masknet':
            mask_pred = model(data)
            mask_gt = data.mask.unsqueeze(1)
            loss = torch.nn.functional.binary_cross_entropy_with_logits(mask_pred, mask_gt.float(), reduction='mean')
        elif args.arch == 'jointnet':
            data_displacement = model(data)
            y_pred = data_displacement + data.pos
            loss = 0.0
            for i in range(args.train_batch):
                joint_gt = data.joints[data.joints_batch == i, :]
                y_pred_i = y_pred[data.batch == i, :]
                loss += chamfer_distance_with_average(y_pred_i.unsqueeze(0), joint_gt.unsqueeze(0))
            loss /= args.train_batch
        loss.backward()
        optimizer.step()
        loss_meter.update(loss.item())
    return loss_meter.avg


def test(test_loader, model, args, save_result=False, best_epoch=None):
    global device
    model.eval()  # switch to test mode
    loss_meter = AverageMeter()
    outdir = args.checkpoint.split('/')[-1]
    for data in test_loader:
        data = data.to(device)
        with torch.no_grad():
            if args.arch == 'masknet':
                mask_pred = model(data)
                mask_gt = data.mask.unsqueeze(1)
                loss = torch.nn.functional.binary_cross_entropy_with_logits(mask_pred, mask_gt.float(), reduction='mean')
            elif args.arch == 'jointnet':
                data_displacement = model(data)
                y_pred = data_displacement + data.pos
                loss = 0.0
                for i in range(args.test_batch):
                    joint_gt = data.joints[data.joints_batch == i, :]
                    y_pred_i = y_pred[data.batch == i, :]
                    loss += chamfer_distance_with_average(y_pred_i.unsqueeze(0), joint_gt.unsqueeze(0))
                loss /= args.test_batch
            loss_meter.update(loss.item())

            if save_result:
                output_folder = 'results/{:s}/best_{:d}/'.format(outdir, best_epoch)
                if not os.path.exists(output_folder):
                    mkdir_p(output_folder)
                if args.arch == 'masknet':
                    mask_pred = torch.sigmoid(mask_pred)
                    for i in range(len(torch.unique(data.batch))):
                        mask_pred_sample = mask_pred[data.batch == i]
                        np.save(os.path.join(output_folder, str(data.name[i].item()) + '_attn.npy'), mask_pred_sample.data.to("cpu").numpy())
                else:
                    for i in range(len(torch.unique(data.batch))):
                        y_pred_sample = y_pred[data.batch == i, :]
                        output_point_cloud_ply(y_pred_sample, name=str(data.name[i].item()),
                                               output_folder='results/{:s}/best_{:d}/'.format(outdir, best_epoch))
    return loss_meter.avg


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PyG DGCNN')
    parser.add_argument('--arch', default='masknet')  # jointnet, masknet
    parser.add_argument('--start-epoch', default=0, type=int, metavar='N', help='manual epoch number (useful on restarts)')
    parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float, metavar='W', help='weight decay (default: 0)')
    parser.add_argument('--gamma', type=float, default=0.2, help='LR is multiplied by gamma on schedule.')
    parser.add_argument('--epochs', default=100, type=int, metavar='N', help='number of total epochs to run')
    parser.add_argument('--lr', '--learning-rate', default=5e-4, type=float, metavar='LR', help='initial learning rate')
    parser.add_argument('--schedule', type=int, nargs='+', default=[], help='Decrease learning rate at these epochs.')
    parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true', help='evaluate model on val/test set')
    parser.add_argument('--input_normal', action='store_true')
    parser.add_argument('--aggr', default='max', type=str)
    ######################
    parser.add_argument('--train_batch', default=2, type=int, metavar='N', help='train batchsize')
    parser.add_argument('--test_batch', default=2, type=int, metavar='N', help='test batchsize')
    parser.add_argument('-c', '--checkpoint', default='checkpoints/test', type=str, metavar='PATH',
                        help='path to save checkpoint (default: checkpoint)')
    parser.add_argument('--logdir', default='logs/test', type=str, metavar='LOG', help='directory to save logs')
    parser.add_argument('--resume', default='', type=str, metavar='PATH', help='path to latest checkpoint (default: none)')
    parser.add_argument('--train_folder', default='/media/zhanxu/4T/ModelResource_RigNetv1_preproccessed/train/',
                        type=str, help='folder of training data')
    parser.add_argument('--val_folder', default='/media/zhanxu/4T/ModelResource_RigNetv1_preproccessed/val/',
                        type=str, help='folder of validation data')
    parser.add_argument('--test_folder', default='/media/zhanxu/4T/ModelResource_RigNetv1_preproccessed/test/',
                        type=str, help='folder of testing data')
    print(parser.parse_args())
    main(parser.parse_args())
