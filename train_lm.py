# SECTION: Necessary imports
import torch
import torch.nn.functional as F
import torch.utils.data as data
import lightning as L
from lightning.pytorch.tuner.tuning import Tuner
from lightning.pytorch.callbacks import ModelCheckpoint, ModelSummary, LearningRateMonitor
from lightning.pytorch.profilers import AdvancedProfiler
from pathlib import Path
from sentencepiece import SentencePieceProcessor
from lightning.pytorch.loggers import CSVLogger
# from transformers import GPT2TokenizerFast

from modules import CausalTransformer
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
        padding_mask = torch.zeros(self.context_length)
        labels = torch.cat([
            tokens[1:],
            torch.tensor([self.data[idx+1][0]], dtype=tokens.dtype)
        ])

        # due to packing pretraining tokens, only the last index may include pad tokens
        # labels = torch.cat([  # insert random token for last label
        #     tokens[1:],
        #     torch.randint(0, self.vocab_size, (1,), dtype=tokens.dtype)
        # ])
        # padding_mask += float('-inf') * tokens.eq(self.pad_id)
        # padding_mask = torch.nan_to_num(padding_mask)

        return tokens, labels, padding_mask
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
        padding_mask = torch.zeros(self.window_length)
        labels = self.data[strided_idx+1:strided_idx+self.window_length+1]
        return tokens, labels, padding_mask
# !SECTION

class Wikitext103Model(CausalTransformer):
    def _calculate_loss(self, batch, sliding=False):
        data, labels, mask = batch
        data = data.int()
        preds = self.forward(data, pad_mask=mask) # shape = [bsz, context_len, vocab_size]
        if sliding:
            preds = preds[:, -1]
            labels = labels[:, -1].long()
            return F.cross_entropy(preds, labels)
        else:
            return F.cross_entropy(preds.view(-1, preds.size(-1)), labels.view(-1).long())

    def training_step(self, batch, batch_idx):
        loss = self._calculate_loss(batch)
        self.log(
            "train_loss",
            loss, 
            sync_dist=True,       # this doesn't seem to impact training time, likely because we have only 3 devices
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
CHECKPOINT_BASE = "./experiments/12_layers_12_heads"
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

    BATCH_SIZE = 64  # NOTE: in '16-mixed', we can use 80
    train_loader = data.DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True, drop_last=True, num_workers=3, pin_memory=True)
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
                dirpath=CHECKPOINT_DIR
            ),
            ModelCheckpoint(
                save_weights_only=False,
                every_n_train_steps=len(train_loader),
                dirpath=CHECKPOINT_DIR,
                filename='last-{epoch:02d}-{step:02d}'
            ),
            # LearningRateMonitor(logging_interval='step')
        ],
        accelerator="gpu",
        devices=3,                 # TODO: Change this back to 3 (David was running an experiment on GPU0)
        strategy="ddp",
        precision="16-mixed",      # TODO: Use 32-true?
        max_epochs=25,
        gradient_clip_val=1.0,     # TODO: change this back to a low value like 1.0 or 0.1
        benchmark=False,           # this can't be used when deterministic=True
        profiler=None,             # AdvancedProfiler(dirpath='./', filename='profile.log'),
        limit_train_batches=None,  # TODO: change this back to None
        limit_val_batches=None,    # TODO: change this back to None
        log_every_n_steps=50       # TODO: change this back to 50
    )
    trainer.logger._default_hp_metric = None  # Optional logging argument that we don't need

    # Check whether a checkpoint exists in this directory. If so, exit with error as to not overwrite any data due to missed input.
    if len(list(checkpoint_path.glob('*.ckpt'))) == 0:
        model = Wikitext103Model(
            num_classes=len(tokenizer),
            max_context_len=512,
            model_dim=768,
            attention_norm=None,                                                  # Use None for dot-product attention, 1 for Manhattan, or 2 for Euclidean
            learn_temperatures=False,
            positional_temperatures=False,
            num_heads=12,
            num_layers=12,
            dropout=0.1,
            attn_dropout=0.1,
            activation_dropout=0.1,
            ffn_dim=3072,
            use_pos_encoding=True,
            lr=1e-4,                                                              # used for AdamW/Lion initialization
            num_steps=trainer.max_epochs*len(train_loader)/trainer.num_devices,   # used for REX Scheduler
            temperature_lr_scale=0.0                                              # sets lr for temperature params to scale*lr
        )
        trainer.fit(model, train_loader, val_loader)
    else:
        raise ValueError(f"Directory {checkpoint_path} already contains pretrained model checkpoints!")
#!SECTION