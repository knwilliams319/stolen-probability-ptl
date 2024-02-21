# SECTION: Necessary imports
import torch
import torch.nn.functional as F
import torch.utils.data as data
import lightning as L
from lightning.pytorch.tuner.tuning import Tuner
from lightning.pytorch.callbacks import ModelCheckpoint, ModelSummary, LearningRateMonitor
from lightning.pytorch.strategies import DDPStrategy
from lightning.pytorch.profilers import AdvancedProfiler
from pathlib import Path
from sentencepiece import SentencePieceProcessor
from torch.distributed.algorithms.ddp_comm_hooks.default_hooks import fp16_compress_hook
from lightning.pytorch.loggers import CSVLogger
import shutil
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
        return self.data.size(0)

    def __getitem__(self, idx):
        tokens = self.data[idx]
        labels = torch.cat([  # insert random token for last label
            tokens[1:],
            torch.randint(0, self.vocab_size, (1,), dtype=tokens.dtype)
        ])

        padding_mask = torch.zeros(self.context_length)
        if idx == len(self.data) - 1: # due to packing pretraining tokens, only the last index may include pad tokens
            padding_mask += float('-inf') * self.data[idx].eq(self.pad_id)
            padding_mask = torch.nan_to_num(padding_mask)

        return tokens, labels, padding_mask

class Wikitext103Model(CausalTransformer):
    def _calculate_loss(self, batch):
        data, labels, mask = batch
        data = data.int()
        preds = self.forward(data, pad_mask=mask) # shape = [bsz, context_len, vocab_size]
        loss = F.cross_entropy(preds.view(-1, preds.size(-1)), labels.view(-1).long())
        return loss

    def training_step(self, batch, batch_idx):
        loss = self._calculate_loss(batch)
        self.log(
            "train_loss",
            loss, 
            sync_dist=True,   # this doesn't seem to impact training time, likely because we have only 3 devices
            on_step=True,
            on_epoch=True,
            rank_zero_only=True,  # this seems to slightly speed up training
            prog_bar=True
        )

        # TODO: delete this gradient calculation code if it's too slow
        total_norm = 0.0
        for p in self.parameters():
            if p.grad is not None:
                param_norm = p.grad.detach().data.norm(2)
                total_norm += param_norm.item() ** 2
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
        return loss

    def validation_step(self, batch, batch_idx):
        loss = self._calculate_loss(batch)
        self.log(
            "val_loss",
            loss, 
            sync_dist=True,
            on_step=False,
            on_epoch=True,
            rank_zero_only=True
        )

    def test_step(self, batch, batch_idx):
        loss = self._calculate_loss(batch)
        self.log(
            "test_loss", 
            loss, 
            sync_dist=True,
            on_step=False,
            on_epoch=True,
            rank_zero_only=True
        ) 
#!SECTION
  
# SECTION: Training parameters
# TODO: make these CLI arguments instead of constants 
CHECKPOINT_BASE = "./experiments/embed_dim_256"
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
                every_n_train_steps=1000,
                dirpath=CHECKPOINT_DIR,
                filename='last-{epoch:02d}-{step:02d}'
            ),
            LearningRateMonitor(logging_interval='step')
        ],
        accelerator="gpu",
        devices=3,
        strategy=DDPStrategy(
            static_graph=True,
            gradient_as_bucket_view=True,
            ddp_comm_hook=fp16_compress_hook
        ),
        precision="32-true",       # TODO: Change this back to '16-mixed'
        max_epochs=100,
        gradient_clip_val=2.0,     # TODO: change this back to a low value like 1.0 or 0.1
        benchmark=False,           # this can't be used when deterministic=True
        profiler=None,             # AdvancedProfiler(dirpath='./', filename='profile.log'),
        limit_train_batches=None,  # TODO: change this back to None
        limit_val_batches=None,    # TODO: change this back to None
        log_every_n_steps=10       # TODO: change this back to 50
    )
    trainer.logger._default_hp_metric = None  # Optional logging argument that we don't need

    # Initialize tokenizer
    tokenizer = SentencePieceProcessor(model_file=TOKENIZER_PATH)
    # tokenizer = GPT2TokenizerFast(vocab_file=TOKENIZER_VOCAB, merges_file=TOKENIZER_MERGES)

    # Create dataloaders
    train_dataset = Wikitext103Dataset(TRAIN_PATH, tokenizer.pad_id(), len(tokenizer))
    val_dataset = Wikitext103Dataset(VALID_PATH, tokenizer.pad_id(), len(tokenizer))
    test_dataset = Wikitext103Dataset(TEST_PATH, tokenizer.pad_id(), len(tokenizer))

    # TODO: Why is 32 so much faster in 32-true than a higher batch size? I was able to turn it up as high as 80, but this slowed training down to over an hour per epoch...
    #       Does PyTorchLightning somehow accumulate batches for you if you use a higher batch size than actually fits on the device?
    BATCH_SIZE = 32  # NOTE: in '16-mixed', we can use 80
    train_loader = data.DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True, drop_last=True, num_workers=3, pin_memory=True)
    val_loader = data.DataLoader(val_dataset, batch_size=BATCH_SIZE, shuffle=False, drop_last=False, num_workers=3)
    test_loader = data.DataLoader(test_dataset, batch_size=BATCH_SIZE, shuffle=False, drop_last=False, num_workers=3)

    # Check whether pretrained model exists. If yes, load it and skip training
    pretrained_filename = Path(CHECKPOINT_DIR, "Wikitext103Model.ckpt")
    if pretrained_filename.exists() and pretrained_filename.is_file():
        print("Found pretrained model, loading...")
        model = Wikitext103Model.load_from_checkpoint(pretrained_filename)
    else:
        model = Wikitext103Model(
            num_classes=len(tokenizer),
            max_context_len=1024,
            model_dim=256,
            use_euclidean_attention=False,
            learn_temperatures=False,
            positional_temperatures=False,
            num_heads=8,
            num_layers=16,
            dropout=0.0,
            attn_dropout=0.0,
            activation_dropout=0.0,
            ffn_dim=2048,
            use_pos_encoding=True,
            lr=3e-4,                                                             # used for AdamW/Lion initialization
            num_steps=trainer.max_epochs*len(train_loader)/trainer.num_devices,  # used for REX Scheduler
            temperature_lr_scale=0.1                                             # sets lr for temperature params to scale*lr
        )
        trainer.validate(model=model, dataloaders=val_loader)
        trainer.fit(model, train_loader, val_loader)
        trainer.validate(model=model, dataloaders=val_loader)

        #tuner = Tuner(trainer)
        #tuner.lr_find(
        #     model,
        #     train_dataloaders=train_loader,
        #     val_dataloaders=val_loader,
        #     early_stop_threshold=None,
        #     num_training=500
        # )
        # model = Wikitext103Model.load_from_checkpoint(trainer.checkpoint_callback.best_model_path)

    # Test best model on validation and test set
    # train_result = trainer.test(model, dataloaders=train_loader, verbose=False)
    # val_result = trainer.test(model, dataloaders=val_loader, verbose=False)
    # test_result = trainer.test(model, dataloaders=test_loader, verbose=False)
    # result = {
    #     "test_acc": test_result[0]["test_acc"],
    #     "val_acc": val_result[0]["test_acc"],
    #     "train_acc": train_result[0]["test_acc"],
    # }
#!SECTION