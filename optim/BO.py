# -*- coding: utf-8 -*-
"""
Created on Thu Jan 23 16:24:31 2020

@author: jacqu

Optimize affinity with bayesian optimization. 

Following tutorial at https://botorch.org/tutorials/vae_mnist

TODO : 
    adapt for affinity (when args.objective == 'aff' )

"""
import os
import sys
import argparse
import torch
import numpy as np
import pandas as pd 

if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.realpath(__file__))
    sys.path.append(os.path.join(script_dir, '..'))
    sys.path.append(os.path.join(script_dir, 'docking'))

import pickle
import torch.utils.data

import torch.nn.utils.clip_grad as clip
import torch.nn.functional as F

from selfies import decoder
from rdkit import Chem

from botorch.models import SingleTaskGP
from gpytorch.mlls.exact_marginal_log_likelihood import ExactMarginalLogLikelihood
from botorch.utils.transforms import standardize, normalize, unnormalize
from botorch.optim import optimize_acqf

from botorch import fit_gpytorch_model
from botorch.acquisition.monte_carlo import qExpectedImprovement
from botorch.sampling.samplers import SobolQMCNormalSampler



if __name__ == "__main__":
    
    from dataloaders.molDataset import Loader
    from model import Model, model_from_json
    from utils import *
    from BO_utils import get_fitted_model
    from docking.docking import dock, set_path

    parser = argparse.ArgumentParser()
    parser.add_argument( '--name', help="saved model weights fname. Located in saved_models subdir",
                        default='kekule')
    parser.add_argument('-n', "--n_steps", help="Nbr of optim steps", type=int, default=50)
    parser.add_argument('-v', '--vocab', default='selfies') # vocab used by model 
    
    parser.add_argument('-o', '--objective', default='aff_pred') # 'qed', 'aff', 'aff_pred'
    
    parser.add_argument('-e', "--ex", help="Docking exhaustiveness (vina)", type=int, default=16) 
    parser.add_argument('-s', "--server", help="COmputer used, to set paths for vina", type=str, default='rup')
    args = parser.parse_args()

    # ==============
    """
    TODO: make it clearer about what goes to gpu and what does not. 
    VAE is on GPU, decoding and aff prediction with MLP are on GPU, but Gaussian process operations on CPU. """

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    # Loader for initial sample
    loader = Loader(props=[], 
                    targets=[], 
                    csv_path = None,
                    vocab=args.vocab, 
                    num_workers = 0,
                    test_only=True)

    # Load model (on gpu if available)
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model = model_from_json(args.name)
    model.to(device)
    model.eval()
    
    d = model.l_size
    dtype = torch.float
    bounds = torch.tensor([[-3.0] * d, [3.0] * d], device='cpu', dtype=dtype)
    BO_BATCH_SIZE = 10
    N_STEPS = args.n_steps
    MC_SAMPLES = 2000
    
    seed=1
    torch.manual_seed(seed)
    best_observed = []
    state_dict = None
    
    # Generate initial data 
    df = pd.read_csv(os.path.join(script_dir,'data','moses_scored_valid.csv'), nrows = 400) # 100 Initial samples 
    
    loader.graph_only=True
    train_z = torch.tensor(model.embed( loader, df)) # z has shape (N_molecules, latent_size)
    
    if args.objective == 'qed':
        scores_init = torch.tensor([Chem.QED.qed(Chem.MolFromSmiles(s)) for s in df.smiles]).view(-1,1).to(device)
    elif args.objective == 'aff_pred':
        with torch.no_grad():
            scores_init = -1* model.affs(train_z.to(device)).cpu() # careful, maximize -aff <=> minimize binding energy (negative value)
    elif args.objective == 'aff' : 
        PYTHONSH, VINA = set_path(args.server)
        scores_init = -1* torch.tensor(df.drd3).view(-1,1).cpu() # careful, maximize -aff <=> minimize binding energy (negative value)
        
    best_value = torch.max(scores_init).item()
    best_observed.append(best_value)
    train_obj = scores_init
    train_smiles = list(df.smiles)
    print(f'-> Best value observed in initial samples : {best_value}')
    
    # Acquisition function 
    def optimize_acqf_and_get_observation(acq_func, device):
        """Optimizes the acquisition function, and returns a new candidate and a noisy observation"""
        
        # optimize
        candidates, _ = optimize_acqf(
            acq_function=acq_func,
            bounds=torch.stack([
                torch.zeros(d, dtype=dtype, device='cpu'), 
                torch.ones(d, dtype=dtype, device='cpu'),
            ]),
            q=BO_BATCH_SIZE,
            num_restarts=10,
            raw_samples=200,
        )
    
        # observe new values 
        new_z = unnormalize(candidates.detach(), bounds=bounds).to(device)
        
        # Decode z into smiles
        with torch.no_grad():
            gen_seq = model.decode(new_z.to(device))
            smiles = model.probas_to_smiles(gen_seq)
        if(args.vocab=='selfies'):
            smiles =[ decoder(s) for s in smiles]
        
        if BO_BATCH_SIZE > 1 : # query a batch of smiles 
            
            if args.objective == 'aff':
                new_scores = torch.zeros((BO_BATCH_SIZE,1), dtype=torch.float)
                for i in range(len(smiles)):
                    _,sc = dock(smiles[i], i, PYTHONSH, VINA, exhaustiveness = args.ex )
                    new_scores[i,0]=sc
                new_scores = -1* new_scores
                
            elif args.objective == 'aff_pred':
                with torch.no_grad():
                    new_scores = -1* model.affs(new_z).cpu() # shape N*1
                    for i in range(len(smiles)):
                        if not isValid(smiles[i]):
                            new_scores[i,0]=0.0 # assigning 0 score to invalid molecules for realistic oracle 
                
            elif args.objective =='qed':
                mols = [Chem.MolFromSmiles(s) for s in smiles ]
                new_scores = torch.zeros(len(smiles), dtype = torch.float)
                for i,m in enumerate(mols):
                    if m!=None:
                        new_scores[i]=Chem.QED.qed(m)
                new_scores = new_scores.unsqueeze(-1)  # new scores must be (N*1)
                
            return smiles, new_z, new_scores
        
        else: #query an individual smiles 
            raise NotImplementedError
    
    
    # ========================================================================
    # run N_BATCH rounds of BayesOpt after the initial random batch
    # ========================================================================
    print('-> Invalid molecules get score 0.0')
    samples_score = [] # avg score of new samples at each step 
    
    for iteration in range(N_STEPS):    
    
        # fit the model
        GP_model = get_fitted_model(
            normalize(train_z, bounds=bounds), 
            standardize(train_obj), 
            state_dict=state_dict,
        )
        
        # define the qNEI acquisition module using a QMC sampler
        qmc_sampler = SobolQMCNormalSampler(num_samples=MC_SAMPLES, seed=seed)
        qEI = qExpectedImprovement(model=GP_model, sampler=qmc_sampler, best_f=standardize(train_obj).max())
    
        # optimize and get new observation
        new_smiles, new_z, new_score = optimize_acqf_and_get_observation(qEI, device)
        
        # save acquired scores for next time 
        print('Iter n° ', iteration, '/', N_STEPS, ' oracle outputs:')
        print(new_score.numpy())
    
        # update training points
        
        train_smiles+= new_smiles 
        train_z = torch.cat((train_z, new_z.cpu()), dim=0)
        train_obj = torch.cat((train_obj, new_score), dim=0)
    
        # update progress
        avg_score = torch.mean(new_score).item()
        best_value, idx = torch.max(train_obj,0)
        samples_score.append(avg_score)
        best_observed.append(best_value.item())
        idx = idx.item()
        best_smiles = train_smiles[idx]
        
        state_dict = GP_model.state_dict()
        
        
        print(f'current best mol: {best_smiles}, with oracle score {best_value.item()}')
        print(f'average score of fresh samples at iter {iteration}: {avg_score}')
        print("\n")
