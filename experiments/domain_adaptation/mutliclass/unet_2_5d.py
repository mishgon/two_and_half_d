from functools import partial
import sys
from pathlib import Path

import numpy as np

import torch
from torch import nn
import torch.nn.functional as F

from dpipe import layers
from dpipe.batch_iter import Infinite, load_by_random_id, apply_at, unpack_args, multiply, combine_pad, random_apply
from dpipe.dataset.wrappers import apply, cache_methods
from dpipe.im import min_max_scale
from dpipe.im.metrics import dice_score, convert_to_aggregated
from dpipe.im.patch import get_random_patch
from dpipe.io import load_json, save_json
from dpipe.predict import add_extract_dims
from dpipe.torch import train_step, save_model_state, inference_step
from dpipe.train import train, TBLogger
from dpipe import commands
from dpipe.train.validator import compute_metrics
from two_and_half_d.batch_iter import get_random_slices

from two_and_half_d.dataset import BraTS2013, ChangeSliceSpacing, CropToBrain
from two_and_half_d.metric import to_binary
from two_and_half_d.torch_module import Quasi3DWrapper, CRFWrapper

from crfseg import CRF


BRATS_PATH = Path(sys.argv[1])
SPLIT_PATH = Path(sys.argv[2])
EXPERIMENT_PATH = Path(sys.argv[3])
FOLD = sys.argv[4]

CONFIG = {
    'source_slice_spacing': 1.,
    'target_slice_spacing': np.linspace(1, 5, 9),
    'n_slices': 15,
    'batch_size': 3,
    'batches_per_epoch': 100,
    'n_epochs': 100,
    'lr': 1e-3,
    'device': 'cuda',
}

try:
    CONFIG.update(load_json(sys.argv[5]))
except (IndexError, FileNotFoundError):
    pass

# dataset
raw_dataset = BraTS2013(BRATS_PATH)
dataset = apply(CropToBrain(raw_dataset), load_image=partial(min_max_scale, axes=0))
train_dataset = cache_methods(ChangeSliceSpacing(dataset, new_slice_spacing=CONFIG['source_slice_spacing']))

# cross validation
split = load_json(SPLIT_PATH)
train_ids, val_ids, test_ids = split[int(FOLD)]

# batch iterator
batch_iter = Infinite(
    load_by_random_id(train_dataset.load_image, train_dataset.load_spacing, train_dataset.load_gt, ids=train_ids),
    unpack_args(get_random_slices, n_slices=CONFIG['n_slices']),
    random_apply(.5, unpack_args(lambda image, spacing, gt: (np.flip(image, 1), spacing, np.flip(gt, 0)))),
    apply_at(1, np.float32),
    batch_size=CONFIG['batch_size'], batches_per_epoch=CONFIG['batches_per_epoch'], combiner=combine_pad
)

# model
unet_2d = nn.Sequential(
    nn.Conv2d(dataset.n_modalities, 8, kernel_size=3, padding=1),
    layers.FPN(
        layers.ResBlock2d, downsample=nn.MaxPool2d(2, ceil_mode=True), upsample=nn.Identity,
        merge=lambda left, down: torch.cat(layers.interpolate_to_left(left, down, 'bilinear'), dim=1),
        structure=[
            [[8, 8, 8], [16, 8, 8]],
            [[8, 16, 16], [32, 16, 8]],
            [[16, 32, 32], [64, 32, 16]],
            [[32, 64, 64], [128, 64, 32]],
            [[64, 128, 128], [256, 128, 64]],
            [[128, 256, 256], [512, 256, 128]],
                    [256, 512, 256]
        ],
        kernel_size=3, padding=1
    ),
    layers.PreActivation2d(8, dataset.n_classes, kernel_size=1)
)

model = CRFWrapper(Quasi3DWrapper(unet_2d), CRF(n_spatial_dims=3, filter_size=5)).to(CONFIG['device'])

criterion = nn.CrossEntropyLoss()
optimizer = torch.optim.Adam(model.parameters(), lr=CONFIG['lr'])


# predict
@unpack_args
@add_extract_dims(1)
def predict(image, spacing):
    return inference_step(image, spacing, architecture=model, activation=nn.Softmax(1))


# metrics
individual_metrics = {f'class {c} dice': to_binary(dice_score, c) for c in range(dataset.n_classes)}
val_metrics = convert_to_aggregated(individual_metrics)


# run experiment
logger = TBLogger(EXPERIMENT_PATH / FOLD / 'logs')
save_json(CONFIG, EXPERIMENT_PATH / 'config.json')
train(train_step, batch_iter, n_epochs=CONFIG['n_epochs'], logger=logger,
      validate=lambda : compute_metrics(predict, lambda i: (train_dataset.load_image(i), train_dataset.load_spacing(i)),
                                        train_dataset.load_gt, val_ids, val_metrics),
      architecture=model, optimizer=optimizer, criterion=criterion, lr=CONFIG['lr'])
save_model_state(model, EXPERIMENT_PATH / FOLD / 'model.pth')

for target_slice_spacing in CONFIG['target_slice_spacing']:
    test_dataset = ChangeSliceSpacing(dataset, new_slice_spacing=target_slice_spacing)
    commands.predict(
        ids=test_ids,
        output_path=EXPERIMENT_PATH / FOLD / f"predictions_{CONFIG['source_slice_spacing']}_to_{target_slice_spacing}",
        load_x=lambda i: (test_dataset.load_image(i), test_dataset.load_spacing(i)),
        predict_fn=predict
    )
    commands.evaluate_individual_metrics(
        load_y_true=test_dataset.load_gt,
        metrics=individual_metrics,
        predictions_path=EXPERIMENT_PATH / FOLD / f"predictions_{CONFIG['source_slice_spacing']}_to_{target_slice_spacing}",
        results_path=EXPERIMENT_PATH / FOLD / f"metrics_{CONFIG['source_slice_spacing']}_to_{target_slice_spacing}"
    )
