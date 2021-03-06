import os
import math
import torch
import random
import editdistance
from argparse import Namespace

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, DistributedSampler
from torch.distributed import is_initialized, get_rank, get_world_size
from torch.nn.utils.rnn import pad_sequence

from examples.speech_recognition.w2l_decoder import W2lKenLMDecoder
from examples.speech_recognition.w2l_decoder import W2lViterbiDecoder

from .model import *
from .dataset import SequenceDataset


def token_to_word(text):
    # Hard coding but it is only used here for now.
    # Assumption that units are characters. Doesn't handle BPE.
    # Inter-character separator is " " and inter-word separator is "|".
    return text.replace(" ", "").replace("|", " ").strip()


def get_decoder(decoder_args_dict, dictionary):
    decoder_args = Namespace(**decoder_args_dict)

    if decoder_args.decoder_type == "viterbi":
        return W2lViterbiDecoder(decoder_args, dictionary)

    elif decoder_args.decoder_type == "kenlm":
        decoder_args.beam_size_token = len(dictionary)
        if isinstance(decoder_args.unk_weight, str):
            decoder_args.unk_weight = eval(decoder_args.unk_weight)
        return W2lKenLMDecoder(decoder_args, dictionary)
    
    else:
        raise ValueError("Only Viterbi or KenLM decoders are supported.")


class DownstreamExpert(nn.Module):
    """
    Used to handle downstream-specific operations
    eg. downstream forward, metric computation, contents to log
    """

    def __init__(self, upstream_dim, upstream_rate, downstream_expert, expdir, **kwargs):
        """
        Args:
            upstream_dim: int
                Different upstream will give different representation dimension
                You might want to first project them to the same dimension

            upstream_rate: int
                160: for upstream with 10 ms per frame
                320: for upstream with 20 ms per frame
            
            downstream_expert: dict
                The 'downstream_expert' field specified in your downstream config file
                eg. downstream/example/config.yaml

            expdir: string
                The expdir from command-line argument, you should save all results into
                this directory, like some logging files.

            **kwargs: dict
                All the arguments specified by the argparser in run_downstream.py
                and all the other fields in config.yaml, in case you need it.
                
                Note1. Feel free to add new argument for __init__ as long as it is
                a command-line argument or a config field. You can check the constructor
                code in downstream/runner.py
        """

        super(DownstreamExpert, self).__init__()
        self.upstream_dim = upstream_dim
        self.upstream_rate = upstream_rate
        self.datarc = downstream_expert['datarc']
        self.modelrc = downstream_expert['modelrc']

        self.train_dataset = SequenceDataset("train", self.datarc['train_batch_size'], **self.datarc)

        self.projector = nn.Linear(upstream_dim, self.modelrc['project_dim'])
        model_cls = eval(self.modelrc['select'])
        model_conf = self.modelrc[self.modelrc['select']]
        self.model = model_cls(
            self.modelrc['project_dim'],
            len(self.train_dataset.symbols),
            upstream_rate,
            **model_conf,
        )
        self.blank = self.train_dataset.dictionary.bos()
        self.objective = nn.CTCLoss(
            blank=self.blank,
            zero_infinity=self.datarc['zero_infinity']
        )
        decoder_args = self.datarc.get('decoder_args')
        self.decoder = None if decoder_args is None else get_decoder(decoder_args, self.train_dataset.dictionary)
        self.dictionary = self.train_dataset.dictionary
        self.register_buffer('best_score', torch.ones(1) * 100)

    # Interface
    def get_dataloader(self, split):
        """
        Args:
            split: string
                The name of the dataloader, can be train/dev/test-clean/test-other for asr

        Return:
            a torch.utils.data.DataLoader returning each batch in the format of:

            [wav1, wav2, ...], your_other_contents1, your_other_contents2, ...

            where wav1, wav2 ... are in variable length
            each wav is torch.FloatTensor in cpu with:
                1. dim() == 1
                2. sample_rate == 16000
                3. directly loaded by torchaudio
        """

        if split == 'train':
            return self._get_train_dataloader(self.train_dataset)
        else:
            if not hasattr(self, f'{split}_dataset'):
                setattr(self, f'{split}_dataset', SequenceDataset(split, self.datarc['eval_batch_size'], **self.datarc))
            return self._get_eval_dataloader(getattr(self, f'{split}_dataset'))


    def _get_train_dataloader(self, dataset):
        sampler = DistributedSampler(dataset) if is_initialized() else None
        return DataLoader(
            dataset, batch_size=1,
            shuffle=(sampler is None),
            sampler=sampler,
            num_workers=self.datarc['num_workers'],
            collate_fn=dataset.collate_fn,
        )


    def _get_eval_dataloader(self, dataset):
        return DataLoader(
            dataset, batch_size=1,
            shuffle=False, num_workers=self.datarc['num_workers'],
            collate_fn=dataset.collate_fn
        )

    def _compute_metrics(self, pred_tokens_batch, pred_words_batch, labels):
        """Computes WER and UER given the prediction and true transcriptions"""
        unit_error_sum = 0.0
        word_error_sum = 0.0
        unit_length_sum = 0
        word_length_sum = 0

        for pred_tokens, pred_words, label in zip(pred_tokens_batch, pred_words_batch, labels):
            label_idx = (label != self.train_dataset.dictionary.pad()) & (
                label != self.train_dataset.dictionary.eos()
            )
            target_token_ids = label[label_idx].tolist()
            target_tokens = self.train_dataset.dictionary.string(target_token_ids)
            target_words = token_to_word(target_tokens).split()

            unit_error_sum += editdistance.eval(pred_tokens, target_tokens)
            unit_length_sum += len(target_token_ids)

            word_error_sum += editdistance.eval(pred_words, target_words)
            word_length_sum += len(target_words)

        uer, wer = 100.0, 100.0
        if unit_length_sum > 0:
            uer = 100.0 * unit_error_sum / unit_length_sum
        if word_length_sum > 0:
            wer = 100.0 * word_error_sum / word_length_sum

        return uer, wer

    def _decode(self, log_probs, input_lens):
        """Decoder that take log probabilities as input and outputs decoded seq"""
        pred_tokens_batch = []
        pred_words_batch = []

        for log_prob, in_len in zip(log_probs, input_lens):
            log_prob = log_prob[:in_len].unsqueeze(0)
            decoded = None
            if self.decoder is not None and not self.training:
                decoded = self.decoder.decode(log_prob)
                if len(decoded) >= 1:
                    decoded = decoded[0]
                    decoded = None if len(decoded) < 1 else decoded[0]
            
            pred_token_ids = log_prob.argmax(dim=-1).unique_consecutive()
            pred_token_ids = pred_token_ids[pred_token_ids != self.blank].tolist()
            pred_tokens = self.train_dataset.dictionary.string(pred_token_ids)

            if decoded is not None and "words" in decoded:
                pred_words = decoded["words"]
            else:
                pred_words = token_to_word(pred_tokens).split()

            pred_tokens_batch.append(pred_tokens)
            pred_words_batch.append(pred_words)

        return pred_tokens_batch, pred_words_batch

    # Interface
    def forward(self, split, features, labels, records, **kwargs):
        """
        Args:
            split: string
                The name of the dataloader, can be train/dev/test-clean/test-other for asr

            features:
                list of unpadded features [feat1, feat2, ...]
                each feat is in torch.FloatTensor and already
                put in the device assigned by command-line args

            your_other_contents1, ... :
                in the order defined by your dataloader (dataset + collate_fn)
                these are all in cpu, and you can move them to the same device
                as features

            records:
                defaultdict(list), by appending contents into records,
                these contents can be averaged and logged on Tensorboard
                later by self.log_records (also customized by you)

                Note1. downstream/runner.py will call self.log_records
                    1. every `log_step` during training
                    2. once after evalute the whole dev/test dataloader

                Note2. `log_step` is defined in your downstream config
                eg. downstream/example/config.yaml

        Return:
            loss:
                the loss to be optimized, should not be detached
                a single scalar in torch.FloatTensor
        """
        device = features[0].device
        features_len = torch.IntTensor([len(feat) for feat in features])
        labels_len = torch.IntTensor([len(label) for label in labels]).to(device=device)
        features = pad_sequence(features, batch_first=True).to(device=device)
        labels = pad_sequence(
            labels,
            batch_first=True,
            padding_value=self.train_dataset.dictionary.pad(),
        ).to(device=device)

        features = self.projector(features)
        log_probs, log_probs_len = self.model(features, features_len)

        loss = self.objective(
                log_probs.transpose(0, 1), # (N, T, C) -> (T, N, C)
                labels,
                log_probs_len,
                labels_len,
            )
        records['loss'].append(loss.item())

        with torch.no_grad():
            pred_tokens_batch, pred_words_batch = self._decode(log_probs.float().contiguous().cpu(), log_probs_len)
            uer, wer = self._compute_metrics(pred_tokens_batch, pred_words_batch, labels)
        records['uer'].append(uer)
        records['wer'].append(wer)

        return loss

    # interface
    def log_records(self, split, records, logger, global_step, batch_ids, total_batch_num, **kwargs):
        """
        Args:
            split: string
                'train':
                    records and batchids contain contents for `log_step` batches
                    `log_step` is defined in your downstream config
                    eg. downstream/example/config.yaml

                'dev' or 'test-clean' or 'test-other' :
                    records and batchids contain contents for the entire evaluation dataset

            records:
                defaultdict(list), contents already prepared by self.forward

            logger:
                Tensorboard SummaryWriter
                please use f'{your_task_name}/{split}-{key}' as key name to log your contents,
                preventing conflict with the logging of other tasks

            global_step:
                The global_step when training, which is helpful for Tensorboard logging

            batch_ids:
                The batches contained in records when enumerating over the dataloader

            total_batch_num:
                The total amount of batches in the dataloader
        
        Return:
            a list of string
                Each string is a filename we wish to use to save the current model
                according to the evaluation result, like the best.ckpt on the dev set
                You can return nothing or an empty list when no need to save the checkpoint
        """
        save_names = []
        for key, values in records.items():
            average = torch.FloatTensor(values).mean().item()
            print(f'{split} {key}: {average}')
            logger.add_scalar(
                f'asr/{split}-{key}',
                average,
                global_step=global_step
            )
            if 'dev-clean' in split and key == 'wer' and average < self.best_score:
                self.best_score = torch.ones(1) * average
                save_names.append(f'{split}-best.ckpt')
        return save_names