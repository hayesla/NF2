import argparse
import json
import logging
import os

import torch
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelCheckpoint, LambdaCallback
from pytorch_lightning.loggers import WandbLogger

from nf2.module import NF2Module, save
from nf2.train.data_loader import SHARPDataModule

parser = argparse.ArgumentParser()
parser.add_argument('--config', type=str, required=True,
                    help='config file for the simulation')
parser.add_argument('--num_workers', type=int, required=False, default=4)
parser.add_argument('--meta_path', type=str, required=False, default=None)
parser.add_argument('--positional_encoding', action='store_true')
parser.add_argument('--use_vector_potential', action='store_true')
args = parser.parse_args()

with open(args.config) as config:
    info = json.load(config)
    for key, value in info.items():
        args.__dict__[key] = value

# data parameters
bin = int(args.bin)
spatial_norm = 320 // bin
height = 320 // bin
b_norm = 2500

# model parameters
dim = args.dim

# training parameters
lambda_div = args.lambda_div
lambda_ff = args.lambda_ff
n_gpus = torch.cuda.device_count()
batch_size = int(args.batch_size)
validation_interval = args.validation_interval
potential = args.potential
num_workers = args.num_workers if args.num_workers is not None else os.cpu_count()
decay_iterations = args.decay_iterations
use_vector_potential = args.use_vector_potential
positional_encoding = args.positional_encoding
iterations = args.iterations

base_path = args.base_path
data_path = args.data_path
work_directory = args.work_directory
work_directory = base_path if work_directory is None else work_directory

os.makedirs(base_path, exist_ok=True)
os.makedirs(work_directory, exist_ok=True)

# init logging
log = logging.getLogger()
log.setLevel(logging.INFO)
for hdlr in log.handlers[:]:  # remove all old handlers
    log.removeHandler(hdlr)
log.addHandler(logging.FileHandler("{0}/{1}.log".format(base_path, "info_log")))  # set the new file handler
log.addHandler(logging.StreamHandler())  # set the new console handler

save_path = os.path.join(base_path, 'extrapolation_result.nf2')

slice = args.slice if 'slice' in args else None

# INIT TRAINING
data_module = SHARPDataModule(data_path,
                              height, spatial_norm, b_norm,
                              work_directory, batch_size, batch_size * 2, iterations, num_workers,
                              potential,
                              slice=slice, bin=bin)

validation_settings = {'cube_shape': data_module.cube_shape,
                 'gauss_per_dB': b_norm,
                 'Mm_per_ds': 320 * 360e-3}

nf2 = NF2Module(validation_settings, dim, positional_encoding, use_vector_potential, lambda_div, lambda_ff,
                decay_iterations,
                args.meta_path)

save_callback = LambdaCallback(on_validation_end=lambda *args: save(save_path, nf2.model, data_module))
checkpoint_callback = ModelCheckpoint(dirpath=base_path, monitor='train/loss',
                                      every_n_train_steps=validation_interval, save_last=True)
logger = WandbLogger(project=args.wandb_project, name=args.wandb_name, offline=False, entity="robert_jarolim")
logger.experiment.config.update({'dim': dim, 'lambda_div': lambda_div, 'lambda_ff': lambda_ff,
                                 'decay_iterations': decay_iterations, 'use_potential': potential,
                                 'use_vector_potential': use_vector_potential})

resume_ckpt = os.path.join(base_path, 'last.ckpt')
resume_ckpt = resume_ckpt if os.path.exists(resume_ckpt) else None

logging.info('Initialize trainer')
trainer = Trainer(max_epochs=2,
                  logger=logger,
                  devices=n_gpus,
                  accelerator='gpu' if n_gpus >= 1 else None,
                  strategy='dp' if n_gpus > 1 else None,  # ddp breaks memory and wandb
                  num_sanity_val_steps=0,
                  val_check_interval=validation_interval,
                  gradient_clip_val=0.1, resume_from_checkpoint=resume_ckpt,
                  callbacks=[checkpoint_callback, save_callback])

logging.info('Start model training')
trainer.fit(nf2, data_module)
save(save_path, nf2.model, data_module)
# clean up
data_module.clear()
