import shutil
import numpy as np
import tqdm
import torch
import torch.nn as nn
from transformers import AdamW
import os
from model import TokenClassification, SequenceClassification
import argparse

def get_args_finetune():
    parser = argparse.ArgumentParser(description='')

    ### mode ###
    parser.add_argument('--task', choices=['melody', 'velocity', 'composer', 'emotion'], required=True)
    ### dataset & data root ###
    parser.add_argument('--dataset', choices=['asap', 'Pianist8',], required=True)
    parser.add_argument('--dataroot', type=str, default=None)
    ### path setup ###
    parser.add_argument('--dict_file', type=str, default='../../Data/Octuple.pkl')
    parser.add_argument('--name', type=str, default='')
    parser.add_argument('--ckpt', default='result/pretrain/musicbert/model.ckpt')

    ### parameter setting ###
    parser.add_argument('--num_workers', type=int, default=5)
    parser.add_argument('--class_num', type=int, default=None)
    parser.add_argument('--batch_size', type=int, default=12)
    parser.add_argument('--max_seq_len', type=int, default=1024, help='all sequences are padded to `max_seq_len`')
    parser.add_argument('--hs', type=int, default=768)
    parser.add_argument("--index_layer", type=int, default=12, help="number of layers")
    parser.add_argument('--epochs', type=int, default=50, help='number of training epochs')
    parser.add_argument('--lr', type=float, default=2e-5, help='initial learning rate')
    parser.add_argument('--nopretrain', action="store_true")  # default: false

    ### cuda ###
    parser.add_argument("--cpu", action="store_true")  # default=False
    parser.add_argument("--cuda_devices", type=int, nargs='+', default=[5,7], help="CUDA device ids")

    args = parser.parse_args()

    # check args
    if args.class_num is None:
        if args.task == 'melody':
            args.class_num = 4
        elif args.task == 'velocity':
            args.class_num = 7
        elif args.task == 'composer':
            args.class_num = 8
        elif args.task == 'emotion':
            args.class_num = 4

    return args


class FinetuneTrainer:
    def __init__(self, midibert, train_dataloader, valid_dataloader, test_dataloader, layer,
                 lr, class_num, hs, testset_shape, cpu, cuda_devices=None, model=None, SeqClass=False):
        device_name = "cuda"
        if cuda_devices is not None and len(cuda_devices) >= 1:
            device_name += ":" + str(cuda_devices[0])
        self.device = torch.device(device_name if torch.cuda.is_available() and not cpu else 'cpu')
        print('   device:', self.device)
        self.midibert = midibert
        self.SeqClass = SeqClass
        self.layer=layer


        if model != None:  # load model
            print('load a fine-tuned model')
            self.model = model.to(self.device)
        else:
            print('init a fine-tune model, sequence-level task?', SeqClass)
            if SeqClass:
                self.model = SequenceClassification(self.midibert, class_num, hs).to(self.device)
            else:
                self.model = TokenClassification(self.midibert, class_num, hs).to(self.device)

        #        for name, param in self.model.named_parameters():
        #            if 'midibert.bert' in name:
        #                    param.requires_grad = False
        #            print(name, param.requires_grad)

        if len(cuda_devices) > 1 and not cpu:
            print("Use %d GPUS" % len(cuda_devices) )
            self.model = nn.DataParallel(self.model, device_ids=cuda_devices)
        elif (len(cuda_devices)  == 1 or torch.cuda.is_available()) and not cpu:
            print("Use GPU" , end=" ")
            print(self.device)
        else:
            print("Use CPU")

        self.train_data = train_dataloader
        self.valid_data = valid_dataloader
        self.test_data = test_dataloader

        self.optim = AdamW(self.model.parameters(), lr=lr, weight_decay=0.01)
        self.loss_func = nn.CrossEntropyLoss(reduction='none')

        self.testset_shape = testset_shape

    def compute_loss(self, predict, target, loss_mask, seq):
        loss = self.loss_func(predict, target)
        if not seq:
            loss = loss * loss_mask
            loss = torch.sum(loss) / torch.sum(loss_mask)
        else:
            loss = torch.sum(loss) / loss.shape[0]
        return loss

    def train(self):
        self.model.train()
        train_loss, train_acc = self.iteration(self.train_data, 0, self.SeqClass)
        return train_loss, train_acc

    def valid(self):
        self.model.eval()
        valid_loss, valid_acc = self.iteration(self.valid_data, 1, self.SeqClass)
        return valid_loss, valid_acc

    def test(self):
        self.model.eval()
        test_loss, test_acc, all_output = self.iteration(self.test_data, 2, self.SeqClass)
        return test_loss, test_acc, all_output

    def iteration(self, training_data, mode, seq):
        pbar = tqdm.tqdm(training_data, disable=False)

        total_acc, total_cnt, total_loss = 0, 0, 0

        if mode == 2:  # testing
            all_output = torch.empty(self.testset_shape)
            cnt = 0

        for x, y in pbar:  # (batch, 512, 768)
            batch = x.shape[0]
            x, y = x.to(self.device), y.to(self.device)  # seq: (batch, 512, 4), (batch) / token: , (batch, 512)

            x=x.long()
            y=y.long()

            # avoid attend to pad word
            if not seq:
                attn = (y != 0).float().to(self.device)  # (batch,512)
            else:
                attn = torch.ones((batch, 1024)).to(self.device)  # attend each of them

            y_hat = self.model.forward(x, attn, self.layer)  # seq: (batch, class_num) / token: (batch, 512, class_num)

            # get the most likely choice with max
            output = np.argmax(y_hat.cpu().detach().numpy(), axis=-1)
            output = torch.from_numpy(output).to(self.device)
            if mode == 2:
                all_output[cnt: cnt + batch] = output
                cnt += batch

            # accuracy
            if not seq:
                acc = torch.sum((y == output).float() * attn)
                total_acc += acc
                total_cnt += torch.sum(attn).item()
            else:
                acc = torch.sum((y == output).float())
                total_acc += acc
                total_cnt += y.shape[0]

            # calculate losses
            if not seq:
                y_hat = y_hat.permute(0, 2, 1)
            loss = self.compute_loss(y_hat, y, attn, seq)
            total_loss += loss.item()

            # udpate only in train
            if mode == 0:
                self.model.zero_grad()
                loss.backward()
                self.optim.step()

        if mode == 2:
            return round(total_loss / len(training_data), 4), round(total_acc.item() / total_cnt, 4), all_output
        return round(total_loss / len(training_data), 4), round(total_acc.item() / total_cnt, 4)

    def save_checkpoint(self, epoch, train_acc, valid_acc,
                        valid_loss, train_loss, is_best, filename):
        state = {
            'epoch': epoch + 1,
            'state_dict': self.model.state_dict(),
            'valid_acc': valid_acc,
            'valid_loss': valid_loss,
            'train_loss': train_loss,
            'train_acc': train_acc,
            'optimizer': self.optim.state_dict()
        }
        torch.save(state, filename)

        best_mdl = filename.split('.')[0] + '_best.ckpt'

        if is_best:
            shutil.copyfile(filename, best_mdl)

def load_data_finetune(dataset, task, data_root=None):
    if data_root is None:
        data_root = 'Data/finetune/others'


    if dataset == 'emotion':
        dataset = 'emopia'

    if dataset not in ['pop909', 'composer', 'emopia', 'asap', 'Pianist8']:
        print(f'Dataset {dataset} not supported')
        exit(1)



    if task=="gen":
        X_train = np.load(os.path.join(data_root, f'{dataset}_train_gen.npy'), allow_pickle=True)
        X_val = np.load(os.path.join(data_root, f'{dataset}_valid_gen.npy'), allow_pickle=True)
        X_test = np.load(os.path.join(data_root, f'{dataset}_test_gen.npy'), allow_pickle=True)

        print('X_train: {}, X_valid: {}, X_test: {}'.format(X_train.shape, X_val.shape, X_test.shape))
        y_train = np.load(os.path.join(data_root, f'{dataset}_train_{task[:3]}ans.npy'), allow_pickle=True)
        y_val = np.load(os.path.join(data_root, f'{dataset}_valid_{task[:3]}ans.npy'), allow_pickle=True)
        y_test = np.load(os.path.join(data_root, f'{dataset}_test_{task[:3]}ans.npy'), allow_pickle=True)
    else:
        X_train = np.load(os.path.join(data_root, f'{dataset}_train.npy'), allow_pickle=True)
        X_val = np.load(os.path.join(data_root, f'{dataset}_valid.npy'), allow_pickle=True)
        X_test = np.load(os.path.join(data_root, f'{dataset}_test.npy'), allow_pickle=True)

        print('X_train: {}, X_valid: {}, X_test: {}'.format(X_train.shape, X_val.shape, X_test.shape))
        if dataset == 'pop909':
            y_train = np.load(os.path.join(data_root, f'{dataset}_train_{task[:3]}comans.npy'), allow_pickle=True)
            y_val = np.load(os.path.join(data_root, f'{dataset}_valid_{task[:3]}comans.npy'), allow_pickle=True)
            y_test = np.load(os.path.join(data_root, f'{dataset}_test_{task[:3]}comans.npy'), allow_pickle=True)
        else:
            y_train = np.load(os.path.join(data_root, f'{dataset}_train_comans.npy'), allow_pickle=True)
            y_val = np.load(os.path.join(data_root, f'{dataset}_valid_comans.npy'), allow_pickle=True)
            y_test = np.load(os.path.join(data_root, f'{dataset}_test_comans.npy'), allow_pickle=True)

    print('y_train: {}, y_valid: {}, y_test: {}'.format(y_train.shape, y_val.shape, y_test.shape))

    return X_train, X_val, X_test, y_train, y_val, y_test