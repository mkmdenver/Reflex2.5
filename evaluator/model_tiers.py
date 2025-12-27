# evaluator/model_tiers.py

from collections import defaultdict

class ModelTierState:
    def __init__(self, model_name):
        self.model = model_name
        self.state = {}

    def get(self, sym):
        return self.state.get(sym, None)

    def set(self, sym, tier):
        self.state[sym] = tier

    def symbols_in(self, *tiers):
        return {s for s, t in self.state.items() if t in tiers}
