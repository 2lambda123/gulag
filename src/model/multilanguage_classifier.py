import logging
from itertools import chain
from typing import Tuple, List, Dict

import gin
import torch.optim
from pytorch_lightning import LightningModule
from pytorch_lightning.utilities.types import STEP_OUTPUT
from torch import Tensor
from torch.nn import functional as F
from torchmetrics import F1Score, MetricCollection
from transformers import AutoModel

from src.model.token_classifier import TokenClassifier

_logger = logging.getLogger(__name__)


@gin.configurable
class MultiLanguageClassifier(LightningModule):
    """Lightning module that encapsulate all routines for multi-language text classification.

    Maybe used as regular Torch module on inference: forward pass returns predicted languages.
    Also support training via lightning Trainer, see: https://pytorch-lightning.readthedocs.io/en/stable/.

    Use HuggingFace models as backbone to embed tokens, e.g. "bert-base-multilingual-cased":
    https://huggingface.co/bert-base-multilingual-cased
    Reports per-token cross-entropy loss and F1-score during training.
    """

    def __init__(
        self, n_languages: int, embedder_name: str = "bert-base-multilingual-cased", *, freeze_embedder: bool = True
    ):
        """Gin configurable constructor for multi-language classifier.

        :param n_languages: number of languages to classify.
        :param embedder_name: name of pretrained HuggingFace model to embed tokens.
        :param freeze_embedder: if `True` then freeze backbone module and train only top classifier.
        """
        super().__init__()

        self._token_embedder = AutoModel.from_pretrained(embedder_name)
        self._token_classifier = TokenClassifier(n_languages, self._token_embedder.config.hidden_size)
        self._n_langs = n_languages

        if freeze_embedder:
            _logger.info(f"Freezing embedding model: {self._token_embedder.__class__.__name__}")
            for param in self._token_embedder.parameters():
                param.requires_grad = False

        self._metric = MetricCollection(
            {f"{split}_f1": F1Score(num_classes=n_languages) for split in ["train", "val", "test"]}
        )

    def forward(self, tokenized_texts: Tensor, attention_mask: Tensor) -> Tensor:  # type: ignore
        """Forward pass of multi-language classification model.
        Could be used during inference to classify each token in text.

        :param tokenized_texts: [batch size; seq len] -- batch with pretokenized texts.
        :param attention_mask: [batch size; seq len] -- attention mask with 0 for padding tokens.
        :return: [batch size; seq len] -- ids of predicted languages.
        """
        # [batch size; seq len; embed dim]
        token_embeddings = self._token_embedder(
            input_ids=tokenized_texts, attention_mask=attention_mask
        ).last_hidden_state
        # [batch size; seq len; embed dim]
        logits = self._token_classifier(token_embeddings)
        # [batch size; seq len]
        top_classes = logits.argmax(dim=-1)
        return top_classes

    @gin.configurable
    def configure_optimizers(self, optimizer_cls=gin.REQUIRED, scheduler_cls=None):
        """Gin configurable method to define optimizers and learning rate scheduler.
        Gin should define classes that would be initialized here.

        Both optimizer and scheduler are also configured by gin.

        :param optimizer_cls: PyTorch optimizer class, e.g. `torch.optim.AdamW`.
        :param scheduler_cls: PyTorch scheduler class, e.g. `torch.optim.lr_scheduler.LambdaLR`.
                                If `None`, then constant lr.
        """
        parameters = chain(self._token_embedder.parameters(), self._token_classifier.parameters())
        optimizer = optimizer_cls(parameters)
        if scheduler_cls is None:
            return optimizer
        lr_scheduler = scheduler_cls(optimizer)
        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": lr_scheduler,
                "interval": "step",
            },
        }

    def shared_step(self, batch: Tuple[Tensor, ...], split: str) -> STEP_OUTPUT:
        """Shared step of them that used during training and evaluation.
        Make forward pass of the model, calculate loss and metric and log them.

        :param batch: Tuple of
            > input_ids [batch size; seq len] – input tokens ids padded to the same length;
            > attention_mask [batch size; seq len] – mask with padding description, 0 means PAD token;
            > labels [batch size; seq len] - labels of each token, `n_languages` used for special tokens.
        :param split: name of current split, one of `train`, `val`, or `test`.
        :return: loss on the current batch.
        """
        input_ids, attention_mask, labels = batch
        bs, seq_len = labels.shape

        embeddings = self._token_embedder(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        logits = self._token_classifier(embeddings)

        logits = logits.view(bs * seq_len, -1)  # [all tokens; n classes]
        labels = labels.view(-1)  # [all tokens]

        # label for padded tokens is `n languages`, `[0; n languages)` used for class ids.
        non_pad_mask = labels < self._n_langs  # [all tokens]
        logits = logits[non_pad_mask]
        labels = labels[non_pad_mask]

        loss = F.cross_entropy(logits, labels)

        with torch.no_grad():
            batch_f1 = self._get_f1_metric(split)(logits, labels)

        if split == "train":
            self.log_dict({"train/step_loss": loss, "train/step_f1": batch_f1})
        return loss

    def training_step(self, batch: Tuple[Tensor, ...], batch_idx: int) -> STEP_OUTPUT:  # type: ignore
        del batch_idx
        return self.shared_step(batch, "train")

    def validation_step(self, batch: Tuple[Tensor, ...], batch_idx: int) -> STEP_OUTPUT:  # type: ignore
        del batch_idx
        return self.shared_step(batch, "val")

    def test_step(self, batch: Tuple[Tensor, ...], batch_idx: int) -> STEP_OUTPUT:  # type: ignore
        del batch_idx
        return self.shared_step(batch, "test")

    def shared_epoch_end(self, epoch_outputs: List[Tensor], split: str):
        mean_loss = torch.stack(epoch_outputs).mean()
        epoch_f1 = self._get_f1_metric(split).compute()
        self._get_f1_metric(split).reset()

        self.log_dict({f"{split}/loss": mean_loss, f"{split}/f1": epoch_f1})

    def training_epoch_end(self, epoch_outputs: List[Dict[str, Tensor]]):  # type: ignore
        self.shared_epoch_end([eo["loss"] for eo in epoch_outputs], "train")

    def validation_epoch_end(self, epoch_outputs: List[Tensor]):  # type: ignore
        self.shared_epoch_end(epoch_outputs, "val")

    def test_epoch_end(self, epoch_outputs: List[Tensor]):  # type: ignore
        self.shared_epoch_end(epoch_outputs, "test")

    def _get_f1_metric(self, split: str) -> F1Score:
        return self._metric[f"{split}_f1"]
