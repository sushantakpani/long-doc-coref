import torch
import torch.nn as nn
from pytorch_utils.modules import MLP


class BaseFixedMemory(nn.Module):
    def __init__(self, num_cells=10, hsize=300, mlp_size=200, mlp_depth=1,
                 mem_size=None, drop_module=None, emb_size=20, entity_rep='max',
                 use_last_mention=False,
                 **kwargs):
        super(BaseFixedMemory, self).__init__()
        # self.query_mlp = query_mlp
        self.hsize = hsize
        self.num_cells = num_cells
        self.mem_size = (mem_size if mem_size is not None else hsize)
        self.mlp_size = mlp_size
        self.emb_size = emb_size
        self.entity_rep = entity_rep

        self.drop_module = drop_module

        self.action_str_to_idx = {'c': 0, 'o': 1, 'i': 2, '<s>': 3}
        self.action_idx_to_str = ['c', 'o', 'i']

        self.use_last_mention = use_last_mention

        if self.entity_rep == 'lstm':
            self.mem_rnn = nn.LSTMCell(
                input_size=self.mem_size,
                hidden_size=self.mem_size)
        elif self.entity_rep == 'gru':
            self.mem_rnn = nn.GRUCell(
                input_size=self.mem_size,
                hidden_size=self.mem_size)

        # CHANGE THIS PART
        self.query_projector = nn.Linear(self.hsize + 2 * self.emb_size, self.mem_size)

        self.mem_coref_mlp = MLP(3 * self.mem_size + 2 * self.emb_size, self.mlp_size, 1,
                                 num_layers=mlp_depth, bias=True, drop_module=drop_module)
        if self.use_last_mention:
            self.ment_coref_mlp = MLP(3 * self.mem_size, self.mlp_size, 1,
                                      num_layers=mlp_depth, bias=True, drop_module=drop_module)

        self.mem_fert_mlp = MLP(input_size=self.mem_size + 2 * emb_size,
                                hidden_size=self.mlp_size, output_size=1, num_layers=mlp_depth,
                                bias=True, drop_module=drop_module)
        self.ment_fert_mlp = MLP(input_size=self.mem_size, hidden_size=self.mlp_size,
                                 output_size=1, num_layers=mlp_depth, bias=True, drop_module=drop_module)

        self.last_action_emb = nn.Embedding(4, self.emb_size)
        self.distance_embeddings = nn.Embedding(11, self.emb_size)
        self.width_embeddings = nn.Embedding(30, self.emb_size)
        self.counter_embeddings = nn.Embedding(11, self.emb_size)

    @staticmethod
    def get_distance_bucket(dist):
        assert (dist >= 0)
        if dist < 5:
            return dist + 1
        elif 5 <= dist <= 7:
            return 6
        elif 8 <= dist <= 15:
            return 7
        elif 16 <= dist <= 31:
            return 8
        elif 32 <= dist <= 63:
            return 9

        return 10

    @staticmethod
    def get_counter_bucket(count):
        assert (count >= 0)
        if count <= 5:
            return count
        elif 5 < count <= 7:
            return 6
        elif 8 <= count <= 15:
            return 7
        elif 16 <= count <= 31:
            return 8
        elif 32 <= count <= 63:
            return 9

        return 10

    @staticmethod
    def get_mention_width_bucket(width):
        if width < 29:
            return width

        return 29

    def initialize_memory(self):
        """Initialize the memory to null."""
        mem = torch.zeros(self.num_cells, self.mem_size).cuda()
        ent_counter = torch.tensor([0 for i in range(self.num_cells)]).cuda()
        last_mention_idx = [0 for _ in range(self.num_cells)]
        return mem, ent_counter, last_mention_idx

    @staticmethod
    def get_coref_mask(ent_counter):
        cell_mask = (ent_counter > 0.0).float().cuda()
        return cell_mask

    def get_overwrite_ign_mask(self, ent_counter):
        last_unused_cell = None
        for cell_idx, cell_count in enumerate(ent_counter.tolist()):
            if cell_count == 0:
                last_unused_cell = cell_idx

        if last_unused_cell:
            score_arr = ([0] * last_unused_cell + [1]
                         + [0] * (self.num_cells - last_unused_cell))
            # Return the overwrite probability vector combined with ignore prob (last entry)
            return torch.tensor(score_arr).cuda().float()
        else:
            return torch.tensor([1] * (1 + self.num_cells)).cuda().float()

    def get_all_mask(self, ent_counter):
        coref_mask = self.get_coref_mask(ent_counter)
        overwrite_ign_mask = self.get_overwrite_ign_mask(ent_counter)
        return torch.cat([coref_mask, overwrite_ign_mask], dim=0)

    def get_distance_emb(self, ment_idx, last_mention_idx):
        distance_buckets = [self.get_distance_bucket(ment_idx - last_mention_idx[cell_idx])
                            for cell_idx in range(self.num_cells)]
        distance_tens = torch.tensor(distance_buckets).long().cuda()
        distance_embs = self.distance_embeddings(distance_tens)
        return distance_embs

    def get_counter_emb(self, ent_counter):
        counter_buckets = [self.get_counter_bucket(ent_counter[cell_idx])
                           for cell_idx in range(self.num_cells)]
        counter_tens = torch.tensor(counter_buckets).long().cuda()
        counter_embs = self.counter_embeddings(counter_tens)
        return counter_embs

    def get_last_action_emb(self, action_str):
        action_emb = self.action_str_to_idx[action_str]
        return self.last_action_emb(torch.tensor(action_emb).cuda())

    def get_coref_new_log_prob(self, query_vector, mem_vectors, last_ment_vectors,
                               ent_counter, distance_embs, counter_embs):
        # Repeat the query vector for comparison against all cells
        rep_query_vector = query_vector.repeat(self.num_cells, 1)  # M x H

        # Coref Score
        pair_vec = torch.cat([mem_vectors, rep_query_vector, mem_vectors * rep_query_vector,
                              distance_embs, counter_embs], dim=-1)
        pair_score = self.mem_coref_mlp(pair_vec)
        coref_score = torch.squeeze(pair_score, dim=-1)  # M

        if self.use_last_mention:
            last_ment_vec = torch.cat(
                [last_ment_vectors, rep_query_vector,
                 last_ment_vectors * rep_query_vector], dim=-1)
            last_ment_score = torch.squeeze(self.ment_coref_mlp(last_ment_vec), dim=-1)
            coref_score = coref_score + last_ment_score  # M

        coref_new_mask = torch.cat([self.get_coref_mask(ent_counter), torch.tensor([1.0]).cuda()], dim=0)
        coref_new_scores = torch.cat(([coref_score, torch.tensor([0.0]).cuda()]), dim=0)

        coref_new_scores = coref_new_scores * coref_new_mask + (1 - coref_new_mask) * (-1e4)
        coref_new_log_prob = torch.nn.functional.log_softmax(coref_new_scores, dim=0)
        return coref_new_log_prob

    def forward(self, mention_emb_list, actions, mentions, teacher_forcing=False):
        pass
