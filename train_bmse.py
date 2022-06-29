import numpy as np
from numpy import load
import random

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data.sampler import SubsetRandomSampler, SequentialSampler

import dgl
import dgl.nn as dglnn
import dgl.data
import dgl.function as fn
from dgl.nn import GraphConv
from dgl.data import DGLDataset
from dgl import save_graphs
from dgl.dataloading import GraphDataLoader

import time

from sklearn.metrics import mean_squared_error

import json
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
import matplotlib.transforms as mtransforms

# Define the Balanced MSE loss (Ren et al., 2022).

def bmc_loss(pred, target, noise_var):
    """Compute the Balanced MSE Loss (BMC) between `pred` and the ground truth `targets`.
    Args:
      pred: A float tensor of size [batch, 1].
      target: A float tensor of size [batch, 1].
      noise_var: A float number or tensor.
    Returns:
      loss: A float tensor. Balanced MSE Loss.
    """
    logits = - (pred - target.T).pow(2) / (2 * noise_var)   # logit size: [batch, batch]
    loss = F.cross_entropy(logits, torch.arange(pred.shape[0]))     # contrastive-like loss
    loss = loss * (2 * noise_var)  # optional: restore the loss scale, 'detach' when noise is learnable 
    return loss

for lead_time in [1]: # [1, 2, 3, 6, 12, 23]

    # GNN configurations
    
    net_class = 'GCN' # 'GAT'
    num_layer = '3'
    num_hid_feat = 200
    num_out_feat = 100
    window_size = 5
    train_split = 0.8
    #lead_time = 1
    loss_function = 'BMSE'
    activiation = 'lrelu' # 'relu', 'tanh' 
    optimizer = 'SGD' # Adam
    learning_rate = 0.02 # 0.05, 0.02, 0.01
    momentum = 0.9
    weight_decay = 0.0001
    batch_size = 64
    num_sample = 1680-window_size-lead_time+1 # max: node_features.shape[1]-window_size-lead_time+1
    num_train_epoch = 30
    
    bmse_noise = 0.2 # 0.5, 0.8
    
    for bmse_noise in [0.2, 0.5, 0.8, 1]:
        
        data_path = 'data/'
        models_path = 'out/'
        out_path = 'out/'
        
        # Transform the graphs into the DGL forms.
        
        node_features = load(data_path + 'node_features.npy')
        edge_features = load(data_path + 'edge_features.npy')
        y = load(data_path + 'y.npy')
        
        node_num = node_features.shape[0]
    
        # DGL Node feature matrix structure
        u = []
        v = []
        for i in range(node_num):
          for j in range(node_num):
            if j == i:
              pass
            else:
              u.append(i)
              v.append(j)
        u = torch.tensor(u)
        v = torch.tensor(v)
        graph = dgl.graph((u, v))
        
        # DGL Node feature matrix
        graph.ndata["feat"] = torch.tensor(node_features)
        
        print("DGL node feature matrix:")
        print(graph.ndata["feat"])
        print("Shape of DGL node feature matrix:")
        print(graph.ndata['feat'].shape)
        print("----------")
        print()
        
        # DGL Edge feature matrix
        w = []
        for i in range(node_num):
          for j in range(node_num):
            if i != j:
              w.append(edge_features[i][j])
        
        graph.edata['w'] = torch.tensor(w)
        
        print("DGL edge feature matrix:")
        print(graph.edata['w'])
        print("Shape of DGL edge feature matrix:")
        print(graph.edata['w'].shape)
        
        print("----------")
        print()
        
        save_graphs(data_path + 'graph.bin', graph)
        
        print("Save the graph in a BIN file.")
        print("--------------------")
        print()
        
        # Create a DGL graph dataset.
        
        class SSTAGraphDataset(DGLDataset):
            def __init__(self):
                super().__init__(name='synthetic')
        
            def process(self):
            
                self.graphs = []
                self.ys = []
                
                for i in range(num_sample):
                  graph_temp = dgl.graph((u, v))
                  graph_temp.ndata['feat'] = torch.tensor(node_features[:, i:i+window_size])
                  graph_temp.edata['w'] = graph.edata['w']
                  self.graphs.append(graph_temp)
                  
                  y_temp = y[i+window_size+lead_time-1]
                  self.ys.append(y_temp)
        
            def __getitem__(self, i):
                return self.graphs[i], self.ys[i]
        
            def __len__(self):
                return len(self.graphs)
        
        dataset = SSTAGraphDataset()
        
        print('Create a DGL dataset: SSTAGraphDataset_windowsize_' + str(window_size) + '_leadtime_' + str(lead_time) + '_trainsplit_' + str(train_split))
        print("The last graph and its label:")
        print(dataset[-1])
        print("--------------------")
        print()
        
        # Create data loaders.
        
        num_examples = len(dataset)
        num_train = int(num_examples * train_split)
        
        # Sequential sampler
        train_dataloader = GraphDataLoader(dataset, sampler=torch.arange(num_train), batch_size=batch_size, drop_last=False)
        test_dataloader = GraphDataLoader(dataset, sampler=torch.arange(num_train, num_examples), batch_size=1, drop_last=False)
        
        it = iter(train_dataloader)
        batch = next(it)
        print("A batch in the traning set:")
        print(batch)
        print("----------")
        print()
        
        it = iter(test_dataloader)
        batch = next(it)
        print("A batch in the test set:")
        print(batch)
        print("----------")
        print()
        
        batched_graph, y = batch
        print('Number of nodes for each graph element in the batch:', batched_graph.batch_num_nodes())
        print('Number of edges for each graph element in the batch:', batched_graph.batch_num_edges())
        print("----------")
        print()
        
        graphs = dgl.unbatch(batched_graph)
        print('The original graphs in the minibatch:')
        print(graphs)
        print("--------------------")
        print()
        
        # Set up GCNs.
        
        # GCN without applying edge features
        class GCN(nn.Module):
            def __init__(self, in_feats, h_feats, out_feats):
                super(GCN, self).__init__()
                self.conv1 = GraphConv(in_feats, h_feats)
                self.conv2 = GraphConv(h_feats, h_feats)
                self.conv3 = GraphConv(h_feats, out_feats)
                self.out = nn.Linear(out_feats, 1)
                self.double()
        
            def forward(self, g, in_feat, edge_feat=None):
                h = self.conv1(g, in_feat)
                h = act_f(h)
                h = self.conv2(g, h)
                h = act_f(h)
                h = self.conv2(g, h)
                h = act_f(h)
                h = self.conv3(g, h)
                h = self.out(h)
                g.ndata['h'] = h
                return dgl.mean_nodes(g, 'h')
        
        # Train the GCN.
        
        model = GCN(window_size, num_hid_feat, num_out_feat)
        optim = torch.optim.SGD(model.parameters(), lr=learning_rate, momentum=momentum, weight_decay=weight_decay)
    
        if activiation == 'lrelu':
            act_f = nn.LeakyReLU(0.1)
        elif activiation == 'tanh':
            act_f = nn.Tanh()
        else:
            act_f = nn.ReLu()
        
        print("Start training.")
        print("----------")
        print()
        
        # Start time
        start = time.time()
        
        all_loss = []
        all_eval = []
        
        for epoch in range(num_train_epoch):
            print("Epoch " + str(epoch+1))
            print()
        
            losses = []
            for batched_graph, y in train_dataloader:
                pred = model(batched_graph, batched_graph.ndata['feat'], batched_graph.edata['w']) 
                loss = bmc_loss(pred, y, bmse_noise)
                optim.zero_grad()
                loss.backward()
                optim.step()
                losses.append(loss.cpu().detach().numpy())
            print('Training loss:', sum(losses) / len(losses))
            all_loss.append(sum(losses) / len(losses))
        
            preds = []
            ys = []
            for batched_graph, y in test_dataloader:
                pred = model(batched_graph, batched_graph.ndata['feat'], batched_graph.edata['w']) ###
                preds.append(pred.cpu().detach().numpy().squeeze(axis=0))
                ys.append(y.cpu().detach().numpy().squeeze(axis=0))
            val_mse = mean_squared_error(np.array(ys), np.array(preds), squared=True)
            print('Validation MSE:', val_mse)
            all_eval.append(val_mse)
        
            print("----------")
            print()
        
        # End time
        stop = time.time()
        
        print(f'Complete training. Time spent: {stop - start} seconds.')
        print("----------")
        print()
        
        # Save the model.
        
        torch.save({
                    'epoch': num_train_epoch,
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optim.state_dict(),
                    'loss': loss
                    }, models_path + 'checkpoint_SSTAGraphDataset_' + str(net_class) + '_' + str(num_hid_feat) + '_' + str(num_out_feat) + '_' + str(window_size) + '_' + str(lead_time) + '_' + str(num_sample) + '_' + str(train_split) + '_' + str(loss_function) + '_' + str(bmse_noise) + '_' + str(optimizer) + '_' + str(activiation) + '_' + str(learning_rate) + '_' + str(momentum) + '_' + str(weight_decay) + '_' + str(batch_size) + '_' + str(num_train_epoch) + '.tar')
        
        print("Save the checkpoint in a TAR file.")
        print("----------")
        print()
    
        # Test the model.
        
        preds = []
        ys = []
        for batched_graph, y in test_dataloader:
            pred = model(batched_graph, batched_graph.ndata['feat'])
            preds.append(pred.cpu().detach().numpy().squeeze(axis=0))
            ys.append(y.cpu().detach().numpy().squeeze(axis=0))
        
        test_mse = mean_squared_error(np.array(ys), np.array(preds), squared=True)
        test_rmse = mean_squared_error(np.array(ys), np.array(preds), squared=False)
        
        print('Final validation / test MSE:', test_mse)
        print("----------")
        print() 
        
        # Show the results.
    
        all_loss = np.array(all_loss)
        all_eval = np.array(all_eval)
        all_epoch = np.array(list(range(1, num_train_epoch+1)))
    
        all_perform_dict = {
          'training_time': str(stop-start),
          'all_loss': all_loss.tolist(),
          'all_eval': all_eval.tolist(),
          'all_epoch': all_epoch.tolist()}
    
        with open(out_path + 'perform_SSTAGraphDataset_' + str(net_class) + '_' + str(num_hid_feat) + '_' + str(num_out_feat) + '_' + str(window_size) + '_' + str(lead_time) + '_' + str(num_sample) + '_' + str(train_split) + '_' + str(loss_function) + '_' + str(bmse_noise) + '_' + str(optimizer) + '_' + str(activiation) + '_' + str(learning_rate) + '_' + str(momentum) + '_' + str(weight_decay) + '_' + str(batch_size) + '_' + str(num_train_epoch) + '.txt', "w") as file:
            file.write(json.dumps(all_perform_dict))
    
        print("Save the performance in a TXT file.")
        print("----------")
        print()
        
        fig, ax = plt.subplots(figsize=(12, 8))
        plt.xlabel('Month')
        plt.ylabel('SSTA')
        plt.title('MSE: ' + str(round(test_mse, 4)), fontsize=12)
        patch_a = mpatches.Patch(color='C0', label='Predicted')
        patch_b = mpatches.Patch(color='C1', label='Observed')
        ax.legend(handles=[patch_a, patch_b])
        month = np.arange(0, len(ys), 1, dtype=int)
        ax.plot(month, np.array(preds), 'o', color='C0')
        ax.plot(month, np.array(ys), 'o', color='C1')
        plt.savefig(out_path + 'pred_a_SSTAGraphDataset_' + str(net_class) + '_' + str(num_hid_feat) + '_' + str(num_out_feat) + '_' + str(window_size) + '_' + str(lead_time) + '_' + str(num_sample) + '_' + str(train_split) + '_' + str(loss_function) + '_' + str(bmse_noise) + '_' + str(optimizer) + '_' + str(activiation) + '_' + str(learning_rate) + '_' + str(momentum) + '_' + str(weight_decay) + '_' + str(batch_size) + '_' + str(num_train_epoch) + '.png')
    
        fig, ax = plt.subplots(figsize=(12, 8))
        ax.set_xlim([-2.5, 2.5])
        ax.set_ylim([-2.5, 2.5])
        plt.xlabel('Observation')
        plt.ylabel('Prediction')
        plt.title('MSE: ' + str(round(test_mse, 4)), fontsize=12)
        ax.plot(np.array(ys), np.array(preds), 'o', color='C0')
        line = mlines.Line2D([0, 1], [0, 1], color='red')
        transform = ax.transAxes
        line.set_transform(transform)
        ax.add_line(line)
        plt.savefig(out_path + 'pred_b_SSTAGraphDataset_' + str(net_class) + '_' + str(num_hid_feat) + '_' + str(num_out_feat) + '_' + str(window_size) + '_' + str(lead_time) + '_' + str(num_sample) + '_' + str(train_split) + '_' + str(loss_function) + '_' + str(bmse_noise) + '_' + str(optimizer) + '_' + str(activiation) + '_' + str(learning_rate) + '_' + str(momentum) + '_' + str(weight_decay) + '_' + str(batch_size) + '_' + str(num_train_epoch) + '.png')
            
        print("Save the observed vs. predicted plots.")
        print("----------")
        print()
       
        plt.figure()
        plt.plot(all_epoch, all_loss)
        plt.plot(all_epoch, all_eval)
        blue_patch = mpatches.Patch(color='C0', label='Loss: ' + str(loss_function))
        orange_patch = mpatches.Patch(color='C1', label='Validation Metric: ' + 'MSE')
        plt.legend(handles=[blue_patch, orange_patch])
        plt.xlabel('Epoch')
        plt.ylabel('Value')
        plt.title('Performance')
        plt.savefig(out_path + 'perform_SSTAGraphDataset_' + str(net_class) + '_' + str(num_hid_feat) + '_' + str(num_out_feat) + '_' + str(window_size) + '_' + str(lead_time) + '_' + str(num_sample) + '_' + str(train_split) + '_' + str(loss_function) + '_' + str(bmse_noise) + '_' + str(optimizer) + '_' + str(activiation) + '_' + str(learning_rate) + '_' + str(momentum) + '_' + str(weight_decay) + '_' + str(batch_size) + '_' + str(num_train_epoch) + '.png')
    
        print("Save the loss vs. evaluation metric plot.")
        print("--------------------")
        print()