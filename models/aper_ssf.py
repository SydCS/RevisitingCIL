import logging
import numpy as np
import torch
from torch import nn
from torch.serialization import load
from tqdm import tqdm
from torch import optim
from torch.nn import functional as F
from torch.utils.data import DataLoader
from utils.inc_net import (
    IncrementalNet,
    SimpleCosineIncrementalNet,
    MultiBranchCosineIncrementalNet,
    SimpleVitNet,
)
from models.base import BaseLearner
from utils.toolkit import target2onehot, tensor2numpy

# tune the model at first session with ssf, and then conduct simplecil.

num_workers = 8


class Learner(BaseLearner):
    def __init__(self, args):
        super().__init__(args)
        if "ssf" not in args["convnet_type"]:
            raise NotImplementedError("Scale requires Scale backbone")
        if "resnet" in args["convnet_type"]:
            self._network = SimpleCosineIncrementalNet(args, True)
            self.batch_size = 128
            self.init_lr = args.get("init_lr", 0.01)
        else:
            self._network = SimpleVitNet(args, True)
            self.batch_size = args["batch_size"]
            self.init_lr = args["init_lr"]

        self.weight_decay = args.get("weight_decay", 0.0005)
        self.min_lr = args.get("min_lr", 1e-8)
        self.args = args

    def after_task(self):
        self._known_classes = self._total_classes

    def replace_fc(self, trainloader, model, args):
        # replace fc.weight with the embedding average of train data
        model = model.eval()
        embedding_list = []
        label_list = []
        with torch.no_grad():
            for i, batch in enumerate(trainloader):
                (_, data, label) = batch
                data = data.cuda()
                label = label.cuda()
                embedding = model(data)["features"]
                embedding_list.append(embedding.cpu())
                label_list.append(label.cpu())
        embedding_list = torch.cat(embedding_list, dim=0)
        label_list = torch.cat(label_list, dim=0)

        class_list = np.unique(self.train_dataset.labels)
        proto_list = []
        for class_index in class_list:
            # print('Replacing...',class_index)
            data_index = (label_list == class_index).nonzero().squeeze(-1)
            embedding = embedding_list[data_index]
            proto = embedding.mean(0)
            self._network.fc.weight.data[class_index] = proto
        return model

    def incremental_train(self, data_manager):
        self._cur_task += 1
        self._total_classes = self._known_classes + data_manager.get_task_size(
            self._cur_task
        )
        self._network.update_fc(self._total_classes)  # STEP 1
        logging.info(
            "Learning on {}-{}".format(self._known_classes, self._total_classes)
        )

        train_dataset = data_manager.get_dataset(
            np.arange(self._known_classes, self._total_classes),
            source="train",
            mode="train",
        )
        self.train_dataset = train_dataset
        self.data_manager = data_manager
        self.train_loader = DataLoader(
            train_dataset,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=num_workers,
        )
        test_dataset = data_manager.get_dataset(
            np.arange(0, self._total_classes), source="test", mode="test"
        )
        self.test_loader = DataLoader(
            test_dataset,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=num_workers,
        )

        train_dataset_for_protonet = data_manager.get_dataset(
            np.arange(self._known_classes, self._total_classes),
            source="train",
            mode="test",
        )
        self.train_loader_for_protonet = DataLoader(
            train_dataset_for_protonet,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=num_workers,
        )

        if len(self._multiple_gpus) > 1:
            print("Multiple GPUs")
            self._network = nn.DataParallel(self._network, self._multiple_gpus)
        self._train(
            self.train_loader, self.test_loader, self.train_loader_for_protonet
        )  # STEP 2
        if len(self._multiple_gpus) > 1:
            self._network = self._network.module

    def _train(self, train_loader, test_loader, train_loader_for_protonet):
        self._network.to(self._device)

        if self._cur_task == 0:  # 第一次 调
            # Freeze the parameters for ViT.
            if "vit" in self.args["convnet_type"]:
                if isinstance(self._network.convnet, nn.Module):
                    for name, param in self._network.convnet.named_parameters():
                        if (
                            "head." not in name
                            and "ssf_scale" not in name
                            and "ssf_shift_" not in name
                        ):
                            param.requires_grad = False
                    print("freezing parameters finished!")
            else:
                if isinstance(self._network.convnet, nn.Module):
                    for name, param in self._network.convnet.named_parameters():
                        if "ssf_scale" not in name and "ssf_shift_" not in name:
                            param.requires_grad = False
                        if param.requires_grad == True:
                            print(name)
                    print("freezing parameters finished!")

            # show total parameters and trainable parameters
            total_params = sum(p.numel() for p in self._network.parameters())
            print(f"{total_params:,} total parameters.")
            total_trainable_params = sum(
                p.numel() for p in self._network.parameters() if p.requires_grad
            )
            print(f"{total_trainable_params:,} training parameters.")
            if total_params != total_trainable_params:
                for name, param in self._network.named_parameters():
                    if param.requires_grad:
                        print(name, param.numel())

            if self.args["optimizer"] == "sgd":
                optimizer = optim.SGD(
                    self._network.parameters(),
                    momentum=0.9,
                    lr=self.init_lr,
                    weight_decay=self.weight_decay,
                )
            elif self.args["optimizer"] == "adam":
                optimizer = optim.AdamW(
                    self._network.parameters(),
                    lr=self.init_lr,
                    weight_decay=self.weight_decay,
                )
            scheduler = optim.lr_scheduler.CosineAnnealingLR(
                optimizer, T_max=self.args["tuned_epoch"], eta_min=self.min_lr
            )
            self._init_train(
                train_loader, test_loader, optimizer, scheduler
            )  # STEP 2.1
            self.construct_dual_branch_network()  # STEP 2.2
        else:
            pass

        # 应该是：FC带着bacbone一起调，调完后把FC替换为NCM
        self.replace_fc(train_loader_for_protonet, self._network, None)  # STEP 2.3

    def construct_dual_branch_network(self):
        network = MultiBranchCosineIncrementalNet(self.args, True)
        network.construct_dual_branch_network(self._network)  # concat
        self._network = network.to(self._device)

    def _init_train(self, train_loader, test_loader, optimizer, scheduler):
        prog_bar = tqdm(range(self.args["tuned_epoch"]))
        for _, epoch in enumerate(prog_bar):
            self._network.train()
            losses = 0.0
            correct, total = 0, 0
            for i, (_, inputs, targets) in enumerate(train_loader):
                inputs, targets = inputs.to(self._device), targets.to(self._device)
                logits = self._network(inputs)["logits"]

                loss = F.cross_entropy(logits, targets)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                losses += loss.item()

                _, preds = torch.max(logits, dim=1)
                correct += preds.eq(targets.expand_as(preds)).cpu().sum()
                total += len(targets)

            scheduler.step()
            train_acc = np.around(tensor2numpy(correct) * 100 / total, decimals=2)

            test_acc = self._compute_accuracy(self._network, test_loader)
            info = "Task {}, Epoch {}/{} => Loss {:.3f}, Train_accy {:.2f}, Test_accy {:.2f}".format(
                self._cur_task,
                epoch + 1,
                self.args["tuned_epoch"],
                losses / len(train_loader),
                train_acc,
                test_acc,
            )
            prog_bar.set_description(info)

        logging.info(info)
