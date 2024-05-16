# SECTION: Necessary imports
from pathlib import Path

import torch
import torch.nn.functional as F
import torch.utils.data as data
import lightning as L
# from lightning.pytorch.tuner.tuning import Tuner
from lightning.pytorch.callbacks import ModelCheckpoint, ModelSummary, LearningRateMonitor
# from lightning.pytorch.profilers import AdvancedProfiler
from lightning.pytorch.loggers import CSVLogger
from sentencepiece import SentencePieceProcessor
from lightning.pytorch.strategies import DDPStrategy

import torch.optim as optim
from modules import CausalTransformer, REXScheduler
#!SECTION

# SECTION: Dataloaders and LightningModules
class Wikitext103Dataset(data.Dataset):
    def __init__(self, tokens_path: str, pad_id: int, vocab_size: int):
        super().__init__()
        self.data = torch.load(tokens_path)
        self.pad_id = pad_id
        self.vocab_size = vocab_size

    @property
    def context_length(self):
        return self.data.size(1)

    def __len__(self):
        # Skip last batch, which is the only incomplete one (due to packing)
        # More importantly, we need label for all tokens of the input, but the last batch can't look ahead
        return self.data.size(0) - 1 

    def __getitem__(self, idx):
        tokens = self.data[idx]
        last_label = self.data[idx+1][0]
        return tokens, last_label # NOTE: due to token packing, pretraining batches will never have padding and we don't need to return a mask
        # labels = torch.cat([
        #     tokens[1:],
        #     torch.tensor([self.data[idx+1][0]], dtype=tokens.dtype)
        # ])
        # return tokens, labels, padding_mask

class FlattenedWikitext103Dataset(data.Dataset):
    def __init__(self, tokens_path: str, pad_id: int, vocab_size: int, stride: int=1, window_length=None):
        super().__init__()
        # Load packed tokens and store other constructor arguments
        self.data = torch.load(tokens_path)
        self.pad_id = pad_id
        self.vocab_size = vocab_size
        self.stride = stride

        # If not provided, default window length is packed tokens' original context length
        self.window_length = window_length if window_length else self.data.size(1) 

        # Flatten packed tokens 
        self.data = torch.flatten(self.data)

        # Find last index at which tokens exist, as there may be padding tokens in the last packed batch
        self.num_tokens = 0
        for i in range(self.data.size(0)):
            if self.data[i] == self.pad_id:
                break
        self.num_tokens = i

    def __len__(self):
        num_windows = self.num_tokens - self.window_length
        divisor, remainder = divmod(num_windows, self.stride) 
        if remainder == 0: 
            return divisor
        else: # if remainder is nonzero, // rounds down to ignore an extra batch that's still within range for labels
            return divisor + 1

    def __getitem__(self, idx):
        strided_idx = idx * self.stride
        tokens = self.data[strided_idx:strided_idx+self.window_length]
        last_label = self.data[strided_idx+self.window_length]
        return tokens, last_label # NOTE: due to token packing, pretraining batches will never have padding and we don't need to return a mask
        # labels = self.data[strided_idx+1:strided_idx+self.window_length+1]
        # return tokens, labels, padding_mask
# !SECTION

class Wikitext103Model(CausalTransformer):
    def configure_optimizers(self):
        # Determine the learning rate for the temperatures' parameter group
        temperature_lr = self.hparams.lr * self.hparams.temperature_lr_scale

        # Split the model's params into temperature and non-temperature groups
        all_params = self.named_parameters()
        base_params = []
        temperature_params = []
        for name, param in all_params:  # objects generated by self.named_parameters() are 2-element tuples (str, torch.Tensor)
            if name.endswith('self_attn.temperatures'):
                temperature_params.append(param)
            else:
                base_params.append(param)

        # Create parameter split dictionary object to pass to optimizers
        param_split = [
            {'params': base_params},
            {'params': temperature_params, 'lr': temperature_lr}
        ]

        optimizer = optim.RAdam(param_split, lr=self.hparams.lr, betas=(0.9, 0.99), eps=1e-6, weight_decay=1e-4)
        scheduler = REXScheduler(optimizer, num_steps=self.hparams.num_steps)
        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": scheduler,
                "interval": "step",
                "frequency": 1
            }
        }

    def _calculate_loss(self, batch, sliding=False):
        data, last_labels = batch
        data = data.int()
        preds = self(data) # shape = [bsz, context_len, vocab_size]
        if sliding:
            preds = preds[:, -1]
            last_labels = last_labels.long()
            return F.cross_entropy(preds, last_labels)
        else:
            labels = torch.cat([data[:, 1:], last_labels.unsqueeze(1)], dim=1)
            return F.cross_entropy(preds.view(-1, preds.size(-1)), labels.view(-1).long())

    def training_step(self, batch, batch_idx):
        loss = self._calculate_loss(batch)
        self.log(
            "train_loss",
            loss, 
            sync_dist=True,        # this doesn't seem to impact training time, likely because we have only 3 devices
            on_step=True,
            on_epoch=True,
            rank_zero_only=False,  # this seems to slightly speed up training
            prog_bar=True
        )

        # calculate norms for total update and layers' updates
        total_norm = 0.0
        layer_grad_norms = [0.0] * self.hparams.num_layers
        for name, p in self.named_parameters():
            if p.grad is not None:
                param_norm = p.grad.detach().data.norm(2).item() ** 2
                total_norm += param_norm
                for i in range(self.hparams.num_layers):
                    if name.startswith(f'transformer.layers.{i}'):
                        layer_grad_norms[i] += param_norm
                        break
        for norm in layer_grad_norms:
            norm = norm ** (1. / 2)
        total_norm = total_norm ** (1. / 2)
        self.log(
            "grad_norm",
            total_norm,
            sync_dist=True,
            on_step=True,
            on_epoch=False,
            rank_zero_only=True,
            prog_bar=True
        )
        for i, norm in enumerate(layer_grad_norms):
            self.log(
            f"layer_norm_{i}",
            norm,
            sync_dist=True,
            on_step=True,
            on_epoch=False,
            rank_zero_only=True,
            prog_bar=False
        )
            
        return loss

    def validation_step(self, batch, batch_idx):
        loss = self._calculate_loss(batch, sliding=True)
        self.log(
            "val_loss",
            loss, 
            sync_dist=True,
            on_step=False,
            on_epoch=True,
            rank_zero_only=False
        )

    def test_step(self, batch, batch_idx):
        loss = self._calculate_loss(batch)
        self.log(
            "test_loss", 
            loss, 
            sync_dist=True,
            on_step=False,
            on_epoch=True,
            rank_zero_only=False
        ) 
#!SECTION
  
# SECTION: Training parameters
# TODO: make these CLI arguments instead of constants 
CHECKPOINT_BASE = "./experiments/embed_dim_512/8_heads/"
EXPERIMENT = "base"
CHECKPOINT_DIR = CHECKPOINT_BASE + '/' + EXPERIMENT

TRAIN_PATH = "./data/wikitext-103/unigram.wiki.train.tokens.tokenized.pt"
VALID_PATH = "./data/wikitext-103/unigram.wiki.valid.tokens.tokenized.pt"
TEST_PATH = "./data/wikitext-103/unigram.wiki.train.tokens.tokenized.pt"
TOKENIZER_PATH = "./unigram-tokenizer/tokenizer.model"
# TOKENIZER_VOCAB = "./data/wikitext-103/tokenizer-vocab.json"
# TOKENIZER_MERGES = "./data/wikitext-103/tokenizer-merges.txt"
#!SECTION
        
# SECTION: Training loop
if __name__ == "__main__":
    # Create checkpoint directory. If it exists, allow user to clear them for a replacement experiment. 
    checkpoint_path = Path(CHECKPOINT_DIR)
    # if checkpoint_path.exists():  # TODO: I like this option, but it gets re-run for each device, so you have to Return 'Y' three times
    #     print(f'Logs exist at {checkpoint_path}! Return `Y` to remove them and continue, or press any other key to exit.')
    #     if input() == 'Y':
    #         shutil.rmtree(checkpoint_path)
    checkpoint_path.mkdir(parents=True, exist_ok=True)

    # Initialize tokenizer
    tokenizer = SentencePieceProcessor(model_file=TOKENIZER_PATH)
    # tokenizer = GPT2TokenizerFast(vocab_file=TOKENIZER_VOCAB, merges_file=TOKENIZER_MERGES)

    # Create dataloaders
    train_dataset = Wikitext103Dataset(TRAIN_PATH, tokenizer.pad_id(), len(tokenizer))
    val_dataset = FlattenedWikitext103Dataset(VALID_PATH, tokenizer.pad_id(), len(tokenizer), stride=256, window_length=512)
    #test_dataset = Wikitext103Dataset(TEST_PATH, tokenizer.pad_id(), len(tokenizer))

    BATCH_SIZE = 13  # NOTE: This is the highest we can go with model_dim=512 and d_k=4
    train_loader = data.DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True, drop_last=True, num_workers=3)
    val_loader = data.DataLoader(val_dataset, batch_size=BATCH_SIZE, shuffle=False, drop_last=False, num_workers=3)
    #test_loader = data.DataLoader(test_dataset, batch_size=BATCH_SIZE, shuffle=False, drop_last=False, num_workers=3)

    # Set up for training. Seed random seeds and initialize Trainer. 
    L.seed_everything(7, workers=True)
    trainer = L.Trainer(
        deterministic=True, 
        default_root_dir=CHECKPOINT_BASE,
        enable_progress_bar=True,
        logger=CSVLogger(
            CHECKPOINT_BASE,
            name='',
            version=EXPERIMENT,
        ),
        callbacks=[
            ModelSummary(),
            ModelCheckpoint(
                save_weights_only=True,
                mode="min", 
                monitor="val_loss",
                dirpath=CHECKPOINT_DIR,
                filename='best-weights-{epoch:02d}'
            ),
            ModelCheckpoint(
                save_weights_only=False,
                every_n_epochs=1,
                dirpath=CHECKPOINT_DIR,
                filename='backup-state-{epoch:02d}'
            ),
            LearningRateMonitor(logging_interval='step')
        ],
        accelerator="gpu",
        devices=3,
        strategy=DDPStrategy(static_graph=True),
        accumulate_grad_batches=1,
        precision="16-mixed",
        max_steps=100000,
        gradient_clip_val=1.0,
        profiler=None,
        limit_train_batches=None,
        limit_val_batches=None,
        log_every_n_steps=100
    )
    trainer.logger._default_hp_metric = None  # Optional logging argument that we don't need

    # Check whether a checkpoint exists in this directory. If so, exit with error as to not overwrite any data due to missed input.
    if len(list(checkpoint_path.glob('*.ckpt'))) == 0:
        model = Wikitext103Model(
            num_classes=len(tokenizer),
            max_context_len=512,
            model_dim=512,
            attention_norm=None,           # Use None for dot-product attention, 1 for Manhattan, or 2 for Euclidean
            learn_temperatures=False,
            positional_temperatures=False,
            num_heads=8,
            num_layers=12,
            dropout=0.1,
            attn_dropout=0.1,
            activation_dropout=0.1,
            ffn_dim=2048,
            use_pos_encoding=True,
            lr=1e-4,                       # used for AdamW/Lion initialization
            num_steps=trainer.max_steps,   # used for REX Scheduler
            temperature_lr_scale=0.0       # sets lr for temperature params to scale*lr
        )
        trainer.fit(model, train_loader, val_loader)
    else:
        raise ValueError(f"Directory {checkpoint_path} already contains pretrained model checkpoints!")
#!SECTION