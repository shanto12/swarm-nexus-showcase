"""Explainable resource policy. Estimates are allowances, never promised cost."""
from __future__ import annotations
import math
import re

POLICY_VERSION = 'nexus-1'

def assess(prompt: str, agent_ceiling=8, token_ceiling=500000):
    text = prompt.lower()
    words = len(prompt.split())
    sources = len(re.findall(r'https?://', text))
    signals = []
    score = min(3, words // 160)
    categories = {
        'Research and source comparison': r'\b(research|compare|sources|investigate|evaluate)\b',
        'Implementation and verification': r'\b(build|implement|deploy|test|architecture|migration)\b',
        'Multiple deliverables or stages': r'\b(report|roadmap|plan|dataset|analysis|strategy)\b',
        'Broad or detailed scope': r'\b(comprehensive|enterprise|production|exhaustive|complex|detailed)\b',
    }
    for reason, pattern in categories.items():
        if re.search(pattern, text):
            score += 1
            signals.append(reason)
    if sources > 1:
        score += min(sources, 3)
        signals.append(f'{sources} supplied sources')
    complexity = 'focused' if score < 2 else 'standard' if score < 4 else 'complex'
    agents = min(agent_ceiling, {'focused': 2, 'standard': 4, 'complex': 8}[complexity])
    allowance = min(token_ceiling, {'focused': 40000, 'standard': 90000, 'complex': 180000}[complexity])
    return {'policy_version': POLICY_VERSION, 'complexity': complexity, 'score': score,
            'recommended_agents': agents, 'initial_token_allocation': allowance,
            'agent_ceiling': agent_ceiling, 'token_ceiling': token_ceiling,
            'basis': 'prompt_estimate', 'reasons': signals or ['A focused deliverable; begin with a small team.'],
            'explanation': 'Initial estimate. The saved plan and ready assignments determine actual concurrency; tokens grow only within your ceiling.'}

def plan_assessment(plan):
    completed, widths, depth = set(), [], 0
    while len(completed) < len(plan):
        ready = {p['id'] for p in plan if p['id'] not in completed and set(p['dependencies']) <= completed}
        if not ready:
            raise ValueError('Cannot assess an invalid dependency graph')
        widths.append(len(ready))
        completed |= ready
        depth += 1
    width = max(widths, default=1)
    return {'basis': 'saved_plan', 'assignments': len(plan), 'layer_width': width, 'dependency_depth': depth,
            'complexity': 'focused' if len(plan) <= 2 else 'standard' if len(plan) <= 6 else 'complex',
            'reasons': [f'{len(plan)} worker assignments', f'Widest dependency layer: {width} assignments', f'{depth} dependency stages'],
            'recommended_tokens': 20000 + len(plan)*22000 + depth*6000}

def growth(current, required, ceiling):
    if required <= current:
        return current
    return min(ceiling, max(current, math.ceil(required/25000)*25000))
