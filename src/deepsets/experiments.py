from typing import List
import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
from torch import optim
from torch.autograd import Variable
from tqdm.auto import tqdm, trange
from IPython import embed
import datetime
import os
from sklearn.manifold import TSNE
from matplotlib import pyplot as plt
from sklearn.metrics import rand_score, adjusted_rand_score, confusion_matrix

from .datasets import MNISTSummation, MNIST_MEAN, MNIST_STD, MNIST_TRANSFORM
from .networks import InvariantModel, SmallMNISTCNNPhi, SmallRho, ClusterClf, OracleClf

from PIL import Image
from torchvision import transforms

from sklearn.metrics import confusion_matrix, log_loss
import seaborn as sns

from matplotlib.offsetbox import OffsetImage, AnnotationBbox
from pathlib import Path

import wandb

def string_for_dict(dict):
    return ','.join([f'{k}:{v}' for k,v in dict.items()])

def tags_for_dict(dict):
    return [f'{k}:{v}' for k,v in dict.items()]

class SumOfDigits(object):
    def __init__(self, lr=1e-3, wd=5e-3, dsize=100000, set_size=500, **kwargs):
        self.lr = lr
        self.wd = wd
        self.set_size = set_size
        # self.train_db = MNISTSummation(min_len=2, max_len=10, dataset_len=dsize, train=True, transform=MNIST_TRANSFORM)
        self.train_db = MNISTSummation(min_len=self.set_size, max_len=self.set_size, dataset_len=dsize, train=True, transform=MNIST_TRANSFORM)
        # self.test_db = MNISTSummation(min_len=5, max_len=50, dataset_len=dsize, train=False, transform=MNIST_TRANSFORM)
        self.test_db = MNISTSummation(min_len=self.set_size, max_len=self.set_size, dataset_len=dsize, train=False, transform=MNIST_TRANSFORM)

        classifier_type = kwargs['classifier_type']
        encoder_type = kwargs['encoder_type']
        model_path = kwargs['model_path']
        self.loss_type = kwargs['loss_type']

        if classifier_type == 'oracle':
            classifier = OracleClf
        elif classifier_type == 'train':
            classifier = ClusterClf
        self.out_dir = Path('.runs') / string_for_dict(kwargs) / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.figures_dir = self.out_dir / 'figures'
        self.checkpoints_dir = self.out_dir / 'checkpoints'
        self.logs_dir = self.out_dir / 'logs'

        self.out_dir.mkdir(exist_ok=True, parents=True)
        self.figures_dir.mkdir(exist_ok=True, parents=True)
        self.checkpoints_dir.mkdir(exist_ok=True, parents=True)
        self.logs_dir.mkdir(exist_ok=True, parents=True)


        self.clf = classifier(input_size=10, output_size=10)
        wandb.init(sync_tensorboard=True, magic=True, tags=tags_for_dict(kwargs))

        self.the_phi = SmallMNISTCNNPhi()
        self.the_rho = SmallRho(input_size=10, output_size=10)

        if encoder_type in ['pretrained', 'finetune']:
            assert model_path is not None, 'Model path is not provided'
            model_path = Path(model_path)
            assert model_path.exists(), f'Model path "{model_path}" does not exist!'
            self.the_phi.load_state_dict(torch.load(model_path / 'trained_phi.pt'))
            self.the_rho.load_state_dict(torch.load(model_path / 'trained_rho.pt'))
        if encoder_type == 'pretrained':
            for param in self.the_phi.parameters():
                param.requires_grad = False
            for param in self.the_rho.parameters():
                param.requires_grad = False


        self.model = InvariantModel(phi=self.the_phi, rho=self.the_rho, clf=self.clf)
        wandb.watch(self.model, log_freq=10, log='all')
        if torch.cuda.is_available():
            self.model.cuda()

        self.optimizer = optim.Adam(self.model.parameters(), lr=self.lr, weight_decay=self.wd)
        # self.optimizer1 = optim.Adam(list(self.the_phi.parameters()) + list(self.the_rho.parameters()), lr=self.lr, weight_decay=self.wd)
        # self.optimizer2 = optim.Adam(self.clf.parameters(), lr=self.lr, weight_decay=self.wd)

        self.summary_writer = SummaryWriter(log_dir= self.logs_dir)

    def train_1_epoch(self, epoch_num: int = 0):
        self.model.train()
        for i in trange(len(self.train_db), leave=False):
            n_train_steps = i + len(self.train_db) * epoch_num
            loss, score = self.train_1_item(i, n_train_steps)
            self.summary_writer.add_scalar('cosine_loss_train', loss, n_train_steps)
            self.summary_writer.add_scalar('rand_score_train', score, n_train_steps)
        x, target = self.train_db.__getitem__(0)
        if torch.cuda.is_available():
            x = x.cuda()

        self.summary_writer.add_graph(self.model, input_to_model=(x, target), verbose=False)

    def train_1_item(self, item_number: int, n_train_steps=None) -> float:
        x, target = self.train_db.__getitem__(item_number)
        if torch.cuda.is_available():
            x, target = x.cuda(), target.cuda()

        x = Variable(x)

        self.optimizer.zero_grad()
        # self.optimizer1.zero_grad()
        # self.optimizer2.zero_grad()

        pred, w = self.model.forward(x, target)
        pred_labels = torch.argmax(w.data, dim=1).cpu().numpy()

        self.summary_writer.add_scalar('cross_entropy_train', log_loss(target.squeeze().detach().cpu().numpy(), w.detach().cpu().numpy()), global_step=n_train_steps)

        target = torch.squeeze(target).cpu().numpy()

        the_loss = self.calculate_loss(pred)
        the_loss.backward()
        self.optimizer.step()
        # if i % 10 == 0:
        #     self.optimizer2.step()
        # else:
        #     self.optimizer1.step()
        # wandb.log({"loss": the_loss})

        the_loss_tensor = the_loss.data
        if torch.cuda.is_available():
            the_loss_tensor = the_loss_tensor.cpu()

        the_loss_numpy = the_loss_tensor.numpy().flatten()
        the_loss_float = float(the_loss_numpy[0])

        score = rand_score(pred_labels, target)
        # score = adjusted_rand_score(pred_labels, target)

        return the_loss_float, score

    def calculate_loss(self, pred):
        cos = torch.nn.CosineSimilarity(dim=0, eps=1e-6)

        the_loss = 0
        for i in range(pred.shape[1]):
            neg_loss = 0
            for j in range(pred.shape[1]):
                if j == i:
                    pos_loss = torch.exp(cos(pred[0, i], pred[1, j]))
                else:
                    neg_loss += torch.exp(cos(pred[0, i], pred[1, j]))
            the_loss += -torch.log(pos_loss / neg_loss)
        return the_loss

    def evaluate(self, epoch):
        self.model.eval()
        # totals = [0] * 51
        # corrects = [0] * 51

        X = np.zeros([len(self.test_db) * 10, 10])
        Y = np.zeros(len(self.test_db) * 10, dtype=int)
        A = np.zeros(len(self.test_db) * self.set_size, dtype=int)
        B = np.zeros(len(self.test_db) * self.set_size, dtype=int)

        total_predictions = []
        total_targets = []
        cluster_input_centroids = [[] for i in range(self.model.rho.output_size)]
        for i in trange(len(self.test_db), leave=False):
            x, target = self.test_db.__getitem__(i)

            item_size = x.shape[0]

            if torch.cuda.is_available():
                x = x.cuda()

            # pred = self.model.forward(Variable(x)).data
            pred, w = self.model.forward(Variable(x), Variable(target))
            pred = pred.data[0]
            pred_labels = torch.argmax(w.data, dim=1)
            A[i * self.set_size: (i + 1) * self.set_size] = pred_labels.cpu().numpy()

            for c in np.arange(10):
                cluster_input_centroids[c].append(x[pred_labels == c, ::].detach().cpu().numpy())

            # if torch.cuda.is_available():
            # pred = pred.cpu().numpy().flatten()
            X[i * 10: (i + 1) * 10] = pred.cpu().numpy()

            # pred = int(round(float(pred[0])))
            # target = int(round(float(target.numpy()[0])))
            B[i * self.set_size: (i + 1) * self.set_size] = torch.squeeze(target).cpu().numpy()
            Y[i * 10: (i + 1) * 10] = np.arange(10)

            total_predictions.append(pred_labels)
            total_targets.append(target)

            # totals[item_size] += 1

            # if pred == target:
            #     corrects[item_size] += 1

        # totals = np.array(totals)
        # corrects = np.array(corrects)

        # print(corrects / totals)

        score = rand_score(A, B)
        # score = adjusted_rand_score(A, B)
        self.summary_writer.add_scalar('rand_score_eval', score, epoch)

        self.record_cluster_embeddings(X, Y, cluster_input_centroids=cluster_input_centroids, epoch=epoch)
        self.record_confusion_matrix(torch.cat(total_predictions), torch.cat(total_targets), epoch)

        torch.save(self.the_phi.state_dict(), self.checkpoints_dir / f'trained_phi_{epoch}.pt')
        torch.save(self.the_rho.state_dict(), self.checkpoints_dir / f'trained_rho_{epoch}.pt')
        torch.save(self.the_phi.state_dict(), self.checkpoints_dir / f'trained_phi.pt')
        torch.save(self.the_rho.state_dict(), self.checkpoints_dir / f'trained_rho.pt')



    def record_cluster_embeddings(self, X, Y, cluster_input_centroids=None, epoch=None):
        tsne = TSNE(n_components=2, random_state=0)
        X_2d = tsne.fit_transform(X)

        target_ids = range(10)
        fig, ax = plt.subplots(figsize=(6, 5))
        colors = 'r', 'g', 'b', 'c', 'm', 'y', 'k', 'brown', 'orange', 'purple'
        for i, c, label in zip(target_ids, colors, target_ids):
            x_coords = X_2d[Y == i, 0]
            y_coords = X_2d[Y == i, 1]
            ax.scatter(x_coords, y_coords, c=c, label=label)

            x_centroid = x_coords.mean()
            y_centroid = y_coords.mean()
            centroid_image = np.vstack(cluster_input_centroids[i]).mean(axis=0)
            centroid_image_unnormalized = centroid_image * MNIST_STD + MNIST_MEAN
            ab = AnnotationBbox(OffsetImage(centroid_image_unnormalized.squeeze()), (x_centroid, y_centroid), frameon=False)
            ax.add_artist(ab)

            # plt.scatter(X[Y == i, 0], X[Y == i, 1], c=c, label=label)

        ax.legend()
        image_path = self.figures_dir / f'tsne_test_{epoch}.png'
        fig.savefig(image_path)
        self.record_image_tensorboard(image_path, 'tsne_embeddings', step=epoch)
        plt.close(fig)

    def record_confusion_matrix(self, pred, target, epoch):
        plt.figure()
        labels = range(10)
        matrix = confusion_matrix(target.cpu().numpy(), pred.cpu().numpy(), labels=labels)
        sns.heatmap(matrix, xticklabels=labels, yticklabels=labels)
        image_path = self.figures_dir / f'conf_matrix_{epoch}.png'
        plt.savefig(image_path)
        self.record_image_tensorboard(image_path, 'confusion_matrix')
        plt.close()

    def record_image_tensorboard(self, image_path, label, step=None):
        img = Image.open(image_path)
        img_tensor = transforms.ToTensor()(img)
        self.summary_writer.add_image(label, img_tensor, global_step=step)
        wandb.log({label : wandb.Image(str(image_path)), 'step': step})
