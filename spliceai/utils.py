from pkg_resources import resource_filename
import pandas as pd
import numpy as np
from pyfaidx import Fasta
from keras.models import load_model
import logging


class Annotator:

    def __init__(self, ref_fasta, annotations):

        if annotations == 'grch37':
            annotations = resource_filename(__name__, 'annotations/grch37.txt')
        elif annotations == 'grch38':
            annotations = resource_filename(__name__, 'annotations/grch38.txt')

        try:
            df = pd.read_csv(annotations, sep='\t', dtype={'CHROM': object})
            self.genes = df['#NAME'].get_values()
            self.chroms = df['CHROM'].get_values()
            self.strands = df['STRAND'].get_values()
            self.tx_starts = df['TX_START'].get_values()+1
            self.tx_ends = df['TX_END'].get_values()
        except IOError:
            logging.error('Gene annotation file {} not found, exiting.'.format(annotations))
            exit()
        except KeyError:
            logging.error('Gene annotation file {} format incorrect, exiting.'.format(annotations))
            exit()

        try:
            self.ref_fasta = Fasta(ref_fasta)
        except IOError:
            logging.error('Reference genome fasta file {} not found, exiting.'.format(ref_fasta))
            exit()

        paths = ('models/spliceai{}.h5'.format(x) for x in range(1, 6))
        self.models = [load_model(resource_filename(__name__, x)) for x in paths]

    def get_name_and_strand(self, chrom, pos):

        chrom = normalise_chrom(chrom, list(self.chroms)[0])
        idxs = np.intersect1d(np.nonzero(self.chroms == chrom)[0],
                              np.intersect1d(np.nonzero(self.tx_starts <= pos)[0],
                              np.nonzero(pos <= self.tx_ends)[0]))

        if len(idxs) >= 1:
            return self.genes[idxs], self.strands[idxs], idxs
        else:
            return [], [], []

    def get_pos_data(self, idx, pos):

        dist_tx_start = self.tx_starts[idx]-pos
        dist_tx_end = self.tx_ends[idx]-pos
        dist = (dist_tx_start, dist_tx_end)

        return dist


def one_hot_encode(seq):

    map = np.asarray([[0, 0, 0, 0],
                      [1, 0, 0, 0],
                      [0, 1, 0, 0],
                      [0, 0, 1, 0],
                      [0, 0, 0, 1]])

    seq = seq.upper().replace('A', '\x01').replace('C', '\x02')
    seq = seq.replace('G', '\x03').replace('T', '\x04').replace('N', '\x00')

    return map[np.fromstring(seq, np.int8) % 5]


def normalise_chrom(source, target):

    def has_prefix(x):
        return x.startswith('chr')

    if has_prefix(source) and not has_prefix(target):
        return source.strip('chr')
    elif not has_prefix(source) and has_prefix(target):
        return 'chr'+source

    return source


def get_delta_scores(record, ann, cov=1001):

    wid = 10000+cov
    delta_scores = []

    try:
        record.chrom, record.pos, record.ref, len(record.alts)
    except TypeError:
        logging.warning('Skipping record (bad input): {}'.format(record))
        return delta_scores

    (genes, strands, idxs) = ann.get_name_and_strand(record.chrom, record.pos)
    if len(idxs) == 0:
        return delta_scores

    chrom = normalise_chrom(record.chrom, list(ann.ref_fasta.keys())[0])
    try:
        seq = ann.ref_fasta[chrom][record.pos-wid//2-1:record.pos+wid//2].seq
    except (IndexError, ValueError):
        logging.warning('Skipping record (fasta issue): {}'.format(record))
        return delta_scores

    if seq[wid//2:wid//2+len(record.ref)].upper() != record.ref:
        logging.warning('Skipping record (ref issue): {}'.format(record))
        return delta_scores

    for j in range(len(record.alts)):
        for i in range(len(idxs)):

            if record.alts[j] == '<NON_REF>' or record.alts[j] == '.':
                continue

            if len(record.ref) > 1 and len(record.alts[j]) > 1:
                delta_scores.append("{}|{}|.|.|.|.|.|.|.|.".format(record.alts[j], genes[i]))
                continue

            dist = ann.get_pos_data(idxs[i], record.pos)
            pad_size = [max(wid//2+dist[0], 0), max(wid//2-dist[1], 0)]
            ref_len = len(record.ref)
            alt_len = len(record.alts[j])
            del_len = max(ref_len-alt_len, 0)

            x_ref = 'N'*pad_size[0]+seq[pad_size[0]:wid-pad_size[1]]+'N'*pad_size[1]
            x_alt = x_ref[:wid//2]+str(record.alts[j])+x_ref[wid//2+ref_len:]

            x_ref = one_hot_encode(x_ref)[None, :]
            x_alt = one_hot_encode(x_alt)[None, :]

            if strands[i] == '-':
                x_ref = x_ref[:, ::-1, ::-1]
                x_alt = x_alt[:, ::-1, ::-1]

            y_ref = np.mean([ann.models[m].predict(x_ref) for m in range(5)], axis=0)
            y_alt = np.mean([ann.models[m].predict(x_alt) for m in range(5)], axis=0)

            if strands[i] == '-':
                y_ref = y_ref[:, ::-1]
                y_alt = y_alt[:, ::-1]

            if ref_len > 1 and alt_len == 1:
                y_alt = np.concatenate([
                    y_alt[:, :cov//2+alt_len],
                    np.zeros((1, del_len, 3)),
                    y_alt[:, cov//2+alt_len:]],
                    axis=1)
            elif ref_len == 1 and alt_len > 1:
                y_alt = np.concatenate([
                    y_alt[:, :cov//2],
                    np.max(y_alt[:, cov//2:cov//2+alt_len], axis=1)[:, None, :],
                    y_alt[:, cov//2+alt_len:]],
                    axis=1)

            y = np.concatenate([y_ref, y_alt])

            idx_pa = (y[1, :, 1]-y[0, :, 1]).argmax()
            idx_na = (y[0, :, 1]-y[1, :, 1]).argmax()
            idx_pd = (y[1, :, 2]-y[0, :, 2]).argmax()
            idx_nd = (y[0, :, 2]-y[1, :, 2]).argmax()

            delta_scores.append("{}|{}|{:.2f}|{:.2f}|{:.2f}|{:.2f}|{}|{}|{}|{}".format(
                                record.alts[j],
                                genes[i],
                                y[1, idx_pa, 1]-y[0, idx_pa, 1],
                                y[0, idx_na, 1]-y[1, idx_na, 1],
                                y[1, idx_pd, 2]-y[0, idx_pd, 2],
                                y[0, idx_nd, 2]-y[1, idx_nd, 2],
                                idx_pa-cov//2,
                                idx_na-cov//2,
                                idx_pd-cov//2,
                                idx_nd-cov//2))

    return delta_scores

def calculate_importance_score(X, models, t):

    Y0 = np.asarray(models[0].predict(X))
    Y1 = np.asarray(models[1].predict(X))
    Y2 = np.asarray(models[2].predict(X))
    Y3 = np.asarray(models[3].predict(X))
    Y4 = np.asarray(models[4].predict(X))
    Y = (Y0+Y1+Y2+Y3+Y4)/5

    if len(Y.shape) == 3:
        Y = Y[None, :]

    WINDOW_SIZE = X.shape[-2]
    L = Y.shape[-2]
    N = 1001
    I = np.zeros((N, X.shape[-3], L, 3))

    for i in range(N):

        Xa = np.copy(X).astype('float')
        Xc = np.copy(X).astype('float')
        Xg = np.copy(X).astype('float')
        Xt = np.copy(X).astype('float')

        Xa[:, (WINDOW_SIZE-N)//2+i] = np.asarray([1, 0, 0, 0])
        Xc[:, (WINDOW_SIZE-N)//2+i] = np.asarray([0, 1, 0, 0])
        Xg[:, (WINDOW_SIZE-N)//2+i] = np.asarray([0, 0, 1, 0])
        Xt[:, (WINDOW_SIZE-N)//2+i] = np.asarray([0, 0, 0, 1])

        I0 = np.asarray(models[0].predict(Xa))+np.asarray(models[0].predict(Xc))+np.asarray(models[0].predict(Xg))+np.asarray(models[0].predict(Xt))
        I1 = np.asarray(models[1].predict(Xa))+np.asarray(models[1].predict(Xc))+np.asarray(models[1].predict(Xg))+np.asarray(models[1].predict(Xt))
        I2 = np.asarray(models[2].predict(Xa))+np.asarray(models[2].predict(Xc))+np.asarray(models[2].predict(Xg))+np.asarray(models[2].predict(Xt))
        I3 = np.asarray(models[3].predict(Xa))+np.asarray(models[3].predict(Xc))+np.asarray(models[3].predict(Xg))+np.asarray(models[3].predict(Xt))
        I4 = np.asarray(models[4].predict(Xa))+np.asarray(models[4].predict(Xc))+np.asarray(models[4].predict(Xg))+np.asarray(models[4].predict(Xt))
        Ii = (I0+I1+I2+I3+I4)/20

        if len(Ii.shape) == 3:
            Ii = Ii[None, :]

        I[i] = Y[t] - Ii[t]

    return I



def get_importance_score(record, ann, cov=1001):

    wid = 10000+cov
    delta_scores = []

    try:
        record.chrom, record.pos, record.ref, len(record.alts)
    except TypeError:
        logging.warning('Skipping record (bad input): {}'.format(record))
        return delta_scores

    (genes, strands, idxs) = ann.get_name_and_strand(record.chrom, record.pos)
    if len(idxs) == 0:
        return delta_scores

    chrom = normalise_chrom(record.chrom, list(ann.ref_fasta.keys())[0])
    try:
        seq = ann.ref_fasta[chrom][record.pos-wid//2-1:record.pos+wid//2].seq
    except (IndexError, ValueError):
        logging.warning('Skipping record (fasta issue): {}'.format(record))
        return delta_scores

    if seq[wid//2:wid//2+len(record.ref)].upper() != record.ref:
        logging.warning('Skipping record (ref issue): {}'.format(record))
        return delta_scores

    for j in range(len(record.alts)):
        for i in range(len(idxs)):

            if record.alts[j] == '<NON_REF>' or record.alts[j] == '.':
                continue

            if len(record.ref) > 1 and len(record.alts[j]) > 1:
                delta_scores.append("{}|{}|.|.|.|.|.|.|.|.".format(record.alts[j], genes[i]))
                continue

            dist = ann.get_pos_data(idxs[i], record.pos)
            pad_size = [max(wid//2+dist[0], 0), max(wid//2-dist[1], 0)]
            ref_len = len(record.ref)
            alt_len = len(record.alts[j])
            del_len = max(ref_len-alt_len, 0)

            x_ref = 'N'*pad_size[0]+seq[pad_size[0]:wid-pad_size[1]]+'N'*pad_size[1]
            x_alt = x_ref[:wid//2]+str(record.alts[j])+x_ref[wid//2+ref_len:]

            x_ref = one_hot_encode(x_ref)[None, :]
            x_alt = one_hot_encode(x_alt)[None, :]

            if strands[i] == '-':
                x_ref = x_ref[:, ::-1, ::-1]
                x_alt = x_alt[:, ::-1, ::-1]

    return calculate_importance_score(x_alt, ann.models, t)
