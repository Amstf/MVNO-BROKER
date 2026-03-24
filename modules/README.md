# modules/

Synthetic capacity and pricing models injected by the broker to emulate time-varying multi-operator dynamics. These are **not** real radio measurements — they produce controlled, reproducible domain conditions for experiments.

All parameters are configured in `conf/background_traffic_gnb.json`.

## Capacity Model (`cap_generator.py`)

Generates a mean-reverting PRB capacity process per domain with stochastic noise and burst events:

```
cap(t+1) = clip( cap(t) + noise(t) + kappa * (baseline - cap(t)) + burst(t) )
```

- `baseline` / `kappa`: mean-reversion target and strength.
- `lam` / `max_step`: Poisson noise intensity and maximum step size.
- `max_cap` / `min_cap` / `floor_frac`: capacity bounds.
- Burst parameters (`burst_rate`, `dur_*`, `depth_*`, `recover`): stochastic transient drops or gains.

The `ScenarioController` divides a run into phases, each with its own baseline and burst config, enabling multi-phase experiments with designed capacity trajectories.

## Pricing Model (`price_model.py`)

Computes a scarcity-based unit PRB price per domain:

```
scarcity(t) = ( cap_base / (cap(t) + eps) ) ^ nu
cost(t)     = scarcity(t) * ( pi_min * min(bmin, used) + pi_be * max(0, used - bmin) )
```

- `cap_base` / `nu` / `eps`: scarcity curve shape.
- `pi_min`: price per guaranteed PRB (up to `bmin`).
- `pi_be`: price per best-effort PRB above `bmin` (typically `pi_be < pi_min`).

## Interaction

Each tick: capacity generator outputs residual PRBs per domain, pricing model converts that into unit cost via the scarcity function, and the broker uses both to decide SLA steering or cost rebalancing.
