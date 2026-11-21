"""
Lone Wolf Labs bot v5 for Builders - Mech Royale (mechfight/1).

Changes vs v4 (scored 2.25):
- Adds real threat scoring: proximity, enemy health, visibility, approach, zone pressure.
- Survival-first movement: escapes zone first, retreats when weak, avoids multiple enemies.
- Avoids charging the nearest enemy when two or more enemies can engage.
- Better target choice: prefers isolated, low-HP targets within effective range.
- Keeps v4's real projectile-speed lead, centre-of-mass aim, fire gate, and loot logic.
- Adds stuck detection and emergency zone escape.
"""

from __future__ import annotations

import array
import json
import math
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf


def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def _dist2d(a: List[float], b: List[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Terrain:
    """Ground height from the map's terrain.i16."""

    def __init__(self, map_dir: str) -> None:
        self.ok = False
        try:
            base = os.path.join(map_dir, "v2")
            with open(os.path.join(base, "world.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)["terrain"]
            self.min_x = float(meta["minX"])
            self.min_y = float(meta["minY"])
            self.step = float(meta["step"])
            self.nx = int(meta["nx"])
            self.ny = int(meta["ny"])
            data = array.array("h")
            with open(os.path.join(base, str(meta.get("file", "terrain.i16"))), "rb") as f:
                data.fromfile(f, self.nx * self.ny)
            if sys.byteorder == "big":
                data.byteswap()
            self.data = data
            self.ok = True
        except Exception:
            self.ok = False

    def ground(self, x: float, y: float) -> Optional[float]:
        if not self.ok:
            return None
        i = max(0, min(self.nx - 1, int(round((x - self.min_x) / self.step))))
        j = max(0, min(self.ny - 1, int(round((y - self.min_y) / self.step))))
        return self.data[j * self.nx + i] / 100.0


class Bot:
    name = "lone-wolf-v5"

    def __init__(self, hello: mf.Hello) -> None:
        cfg = hello.get("config", {})
        loot_cfg = cfg.get("loot", {})
        self.pickup_radius: float = float(loot_cfg.get("pickup_radius", 4.0))
        self.upgrades_cfg: List[Dict[str, Any]] = cfg.get("upgrades", [])
        self.upgrade_priority: Dict[str, int] = {}

        for u in self.upgrades_cfg:
            uid = str(u.get("id", ""))
            effects = u.get("effects", [])
            score = 0
            for ef in effects:
                path = str(ef.get("path", ""))
                if "armor" in uid or "rear" in path or "max_hp" in path or "regen" in path:
                    score += 30
                if "sensor" in uid or "vision" in path or "sound" in path:
                    score += 22
                if "speed" in path or "boost" in path or "energy" in path:
                    score += 18
                if "damage" in path or "rate" in path or "cooldown" in path:
                    score += 12
            self.upgrade_priority[uid] = score

        self.rng = random.Random(int(hello.get("seed", 1234567)))
        self.last_damage_t: float = -999.0
        self.last_damage_bearing: Optional[float] = None
        self.laser_charging: List[bool] = [False, False]
        self.terrain = Terrain(str(hello.get("map", {}).get("dir", "/arena")))
        self.skip_pickups: Dict[int, float] = {}
        self.loot_mem: Optional[Dict[str, Any]] = None
        self.last_pos: Optional[Tuple[float, float]] = None
        self.stuck_ticks: int = 0
        self._strafe_dir: float = self.rng.choice([-1.0, 1.0])
        self._strafe_until: float = 0.0

    def _pick_upgrade(self, offer: Dict[str, Any]) -> int:
        choices = offer.get("choices", [])
        if not choices:
            return 0

        best_idx = 0
        best_score = -10**9

        for idx, ch in enumerate(choices):
            uid = str(ch.get("id", ""))
            desc = str(ch.get("desc", "")).lower()
            s = self.upgrade_priority.get(uid, 0)

            if "armor" in desc or "hp" in desc or "regen" in desc:
                s += 25
            if "vision" in desc or "sensor" in desc or "sound" in desc:
                s += 15
            if "speed" in desc or "energy" in desc:
                s += 12

            if s > best_score:
                best_score = s
                best_idx = idx

        return best_idx

    def _choose_zone_target(self, obs: mf.Observation, x: float, y: float) -> Tuple[float, float]:
        zone = obs["zone"]
        t = float(obs["t"])
        shrink_start = float(zone.get("shrink_start", t))
        shrink_end = float(zone.get("shrink_end", t + 1.0))
        cur_c = zone.get("center", [0.0, 0.0])
        cur_r = float(zone.get("radius", 500.0))
        nxt_c = zone.get("next_center", cur_c)
        nxt_r = float(zone.get("next_radius", cur_r))

        if t >= shrink_start:
            if shrink_end > shrink_start:
                frac = max(0.0, min(1.0, (t - shrink_start) / (shrink_end - shrink_start)))
            else:
                frac = 1.0
            cx = cur_c[0] + (nxt_c[0] - cur_c[0]) * frac
            cy = cur_c[1] + (nxt_c[1] - cur_c[1]) * frac
            rad = cur_r + (nxt_r - cur_r) * frac
        else:
            cx, cy, rad = cur_c[0], cur_c[1], cur_r

        safe_r = max(5.0, rad * 0.68)
        dx_c = nxt_c[0] - cx
        dy_c = nxt_c[1] - cy
        dist_c = math.hypot(dx_c, dy_c)

        if dist_c > 1e-6:
            tx = cx + (dx_c / dist_c) * min(dist_c * 0.70, safe_r * 0.55)
            ty = cy + (dy_c / dist_c) * min(dist_c * 0.70, safe_r * 0.55)
        else:
            tx, ty = cx, cy

        d_me_cur = math.hypot(x - cx, y - cy)
        d_me_next = math.hypot(x - nxt_c[0], y - nxt_c[1])

        if d_me_cur < safe_r and d_me_next > nxt_r * 0.78:
            return float(nxt_c[0]), float(nxt_c[1])

        return float(tx), float(ty)

    def _weapon_fire_decision(
        self,
        slot_idx: int,
        w: Optional[mf.WeaponObs],
        dist: float,
        has_target: bool,
        energy: float,
        target_has_mortar: bool,
    ) -> bool:
        if w is None or not has_target:
            self.laser_charging[slot_idx] = False
            return False

        kind = w.get("kind")
        stats = w.get("stats", {})
        max_range = float(stats.get("range", 200.0))

        if kind == "gun":
            ammo = float(w.get("ammo", 0.0))
            reload_left = float(w.get("reload", 0.0))
            if reload_left > 0.0 or ammo <= 0.0:
                return False
            return dist <= max_range * 0.92

        if kind == "lightning":
            if energy < 12.0 or dist > min(45.0, max_range):
                return False
            return float(w.get("cooldown", 0.0)) <= 0.05

        if kind == "mortar":
            if dist < 24.0 or dist > max_range:
                return False
            return float(w.get("cooldown", 0.0)) <= 0.05

        if kind == "laser":
            charge_time = float(w.get("charge", 0.0))
            full_charge = float(stats.get("charge", 0.75))
            if dist > max_range:
                self.laser_charging[slot_idx] = False
                return False
            target_charge = full_charge * (0.85 if target_has_mortar else 0.98)
            if not self.laser_charging[slot_idx]:
                if float(w.get("cooldown", 0.0)) <= 0.05 and energy > 15.0:
                    self.laser_charging[slot_idx] = True
                    return True
                return False
            if charge_time >= target_charge:
                self.laser_charging[slot_idx] = False
                return False
            return True

        return False

    def _enemy_threat(
        self,
        me_pos: List[float],
        my_hp: float,
        enemy: Dict[str, Any],
        zone_center: List[float],
        zone_radius: float,
    ) -> float:
        x, y, z = me_pos
        ex, ey, ez = enemy["pos"]
        dist = math.hypot(ex - x, ey - y)

        proximity = max(0.0, 1.0 - dist / 85.0)

        enemy_hp = float(enemy.get("hp", 1400.0))
        enemy_max_hp = max(1.0, float(enemy.get("max_hp", 1400.0)))
        enemy_hp_ratio = enemy_hp / enemy_max_hp

        my_hp_ratio = my_hp / max(1.0, float(self._my_max_hp))

        health_risk = max(0.0, enemy_hp_ratio - my_hp_ratio)
        health_risk = min(1.0, health_risk)

        visible = enemy.get("visible", True)
        visibility = 1.0 if visible else 0.30

        vel = enemy.get("vel", [0.0, 0.0, 0.0])
        to_me = [x - ex, y - ey]
        to_me_len = max(1e-6, math.hypot(to_me[0], to_me[1]))
        approach_dot = (vel[0] * to_me[0] + vel[1] * to_me[1]) / to_me_len
        approaching = max(0.0, min(1.0, approach_dot / 8.0))

        my_zone_dist = math.hypot(x - zone_center[0], y - zone_center[1])
        enemy_zone_dist = math.hypot(ex - zone_center[0], ey - zone_center[1])

        zone_risk = max(0.0, 1.0 - my_zone_dist / max(zone_radius, 1e-6))

        if dist < 45.0 and enemy_zone_dist < my_zone_dist:
            zone_risk = min(1.0, zone_risk * 1.5)

        score = (
            0.38 * proximity
            + 0.18 * health_risk
            + 0.20 * visibility
            + 0.12 * approaching
            + 0.12 * zone_risk
        )

        return min(1.0, max(0.0, score))

    def _total_threat(
        self,
        me_pos: List[float],
        my_hp: float,
        enemies: List[Dict[str, Any]],
        zone_center: List[float],
        zone_radius: float,
    ) -> float:
        if not enemies:
            return 0.0

        scores = [
            self._enemy_threat(me_pos, my_hp, enemy, zone_center, zone_radius)
            for enemy in enemies
        ]

        return min(1.0, sum(scores) ** 0.85)

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = float(me["pos"][0]), float(me["pos"][1]), float(me["pos"][2])
        hp = float(me.get("hp", 1400.0))
        max_hp = max(1.0, float(me.get("max_hp", 1400.0)))
        self._my_max_hp = max_hp
        hp_ratio = hp / max_hp
        energy = float(me.get("energy", 0.0))
        in_zone = bool(me.get("in_zone", True))
        t = float(obs["t"])
        my_stats = me.get("stats", {})
        muzzle_z = z + float(my_stats.get("muzzle_height", 7.0))

        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                self.last_damage_t = t
                if ev.get("bearing") is not None:
                    self.last_damage_bearing = float(ev["bearing"])
            elif ev.get("type") == "take_failed":
                self.skip_pickups[int(ev.get("id", -1))] = t + 30.0

        if self.last_pos is not None:
            moved = math.hypot(x - self.last_pos[0], y - self.last_pos[1])
            if moved < 0.12:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = 0
        else:
            self.stuck_ticks = 0

        self.last_pos = (x, y)

        act: mf.Action = {"tick": obs["tick"]}

        if obs.get("offer"):
            act["pick"] = self._pick_upgrade(obs["offer"])  # type: ignore[arg-type]

        weapons = me.get("weapons", [None, None])
        gun_slot = weapons[0] if len(weapons) > 0 and weapons[0] and weapons[0].get("kind") == "gun" else None

        bullet_speed = 252.0
        if gun_slot is not None:
            bullet_speed = max(1.0, float(gun_slot.get("stats", {}).get("speed", 252.0)))
            ammo = float(gun_slot.get("ammo", 0.0))
            mag = float(gun_slot.get("stats", {}).get("mag", 12.0))
            reload_left = float(gun_slot.get("reload", 0.0))
            if reload_left <= 0.0 and ammo <= max(2.0, mag * 0.25):
                act["reload"] = True

        pickups = obs.get("pickups", [])

        if len(weapons) > 1 and weapons[1] is None:
            for p in pickups:
                pkind = p.get("kind")
                if pkind in ("laser", "lightning", "mortar") and _dist2d(p["pos"], [x, y]) <= self.pickup_radius:
                    act["take"] = p["id"]
                    break

        mechs = obs.get("mechs", [])
        hostile_mechs = [m for m in mechs if m.get("kind") == "mech"]

        zone = obs["zone"]
        zone_center = [float(zone.get("center", [0.0, 0.0])[0]), float(zone.get("center", [0.0, 0.0])[1])]
        zone_radius = float(zone.get("radius", 500.0))

        total_threat = self._total_threat(
            me_pos=[x, y, z],
            my_hp=hp,
            enemies=hostile_mechs,
            zone_center=zone_center,
            zone_radius=zone_radius,
        )

        visible_enemies = [m for m in hostile_mechs if m.get("visible", True)]
        nearest_enemy_distance = min(
            (math.hypot(m["pos"][0] - x, m["pos"][1] - y) for m in hostile_mechs),
            default=999.0,
        )

        recently_hit = (t - self.last_damage_t) < 4.5
        low_hp = hp_ratio < 0.48
        very_low_hp = hp_ratio < 0.28

        # Target selection:
        # Prefer isolated, low-HP, close targets. Avoid targets when heavily threatened.
        target: Optional[Dict[str, Any]] = None

        if hostile_mechs:
            def target_score(m: Dict[str, Any]) -> float:
                d = math.dist(m["pos"], [x, y, z])
                enemy_hp_ratio = float(m.get("hp", 1400.0)) / max(1.0, float(m.get("max_hp", 1400.0)))
                threat = self._enemy_threat(
                    me_pos=[x, y, z],
                    my_hp=hp,
                    enemy=m,
                    zone_center=zone_center,
                    zone_radius=zone_radius,
                )

                score = d
                score += enemy_hp_ratio * 70.0
                score += threat * 120.0

                if len(visible_enemies) > 1:
                    score += 80.0

                return score

            sorted_targets = sorted(hostile_mechs, key=target_score)
            target = sorted_targets[0]

            # Do not engage a dangerous target if we are weak or threatened.
            if very_low_hp or total_threat >= 0.78:
                target = None

        # Loot: get second weapon early, but only when relatively safe.
        loot_target: Optional[Dict[str, Any]] = None
        loot_dist = 0.0
        enemy_close = nearest_enemy_distance < 85.0

        if len(weapons) > 1 and weapons[1] is None and in_zone and not enemy_close and total_threat < 0.60:
            held = {w.get("kind") for w in weapons if w}
            best_s = -1e9

            for p in pickups:
                pk = p.get("kind")
                if pk not in ("laser", "lightning", "mortar") or pk in held:
                    continue
                if self.skip_pickups.get(int(p["id"]), -1.0) > t:
                    continue

                g = self.terrain.ground(float(p["pos"][0]), float(p["pos"][1]))
                if g is not None and float(p["pos"][2]) - g > 2.0:
                    continue

                d = _dist2d(p["pos"], [x, y])
                if d > 180.0:
                    continue

                s = {"laser": 40.0, "lightning": 25.0, "mortar": 10.0}[pk] - 0.3 * d

                if s > best_s:
                    best_s, loot_target, loot_dist = s, p, d

        if loot_target is not None:
            self.loot_mem = {
                "id": int(loot_target["id"]),
                "kind": loot_target["kind"],
                "pos": [
                    float(loot_target["pos"][0]),
                    float(loot_target["pos"][1]),
                    float(loot_target["pos"][2]),
                ],
                "seen": t,
            }
        elif self.loot_mem is not None:
            m = self.loot_mem
            d_mem = _dist2d(m["pos"], [x, y])
            gone = (
                len(weapons) <= 1
                or weapons[1] is not None
                or not in_zone
                or enemy_close
                or t - m["seen"] > 6.0
                or d_mem < 40.0
                or self.skip_pickups.get(m["id"], -1.0) > t
            )

            if gone:
                self.loot_mem = None
            else:
                loot_target, loot_dist = m, d_mem

        if loot_target is not None and self.stuck_ticks >= 12:
            self.skip_pickups[int(loot_target["id"])] = t + 15.0
            self.loot_mem = None
            loot_target = None

        zx, zy = self._choose_zone_target(obs, x, y)

        # Default movement: toward safe zone.
        move_x = zx - x
        move_y = zy - y
        move_reason = "zone_safety"

        # If outside or near zone edge, zone escape dominates.
        my_zone_distance = math.hypot(x - zone_center[0], y - zone_center[1])

        if not in_zone or my_zone_distance >= zone_radius * 0.86:
            move_x = zx - x
            move_y = zy - y
            move_reason = "emergency_zone_escape"

        elif very_low_hp and visible_enemies:
            # Retreat away from the closest visible enemy, while drifting inward.
            closest = min(visible_enemies, key=lambda m: math.hypot(m["pos"][0] - x, m["pos"][1] - y))
            away_x = x - closest["pos"][0]
            away_y = y - closest["pos"][1]
            away_len = max(1e-6, math.hypot(away_x, away_y))

            move_x = (away_x / away_len) * 90.0 + (zx - x) * 0.55
            move_y = (away_y / away_len) * 90.0 + (zy - y) * 0.55
            move_reason = "low_hp_retreat"

        elif total_threat >= 0.62 or len(visible_enemies) >= 2:
            # Avoid combat: move away from enemy centroid and toward zone.
            if visible_enemies:
                centroid_x = sum(float(m["pos"][0]) for m in visible_enemies) / len(visible_enemies)
                centroid_y = sum(float(m["pos"][1]) for m in visible_enemies) / len(visible_enemies)
                away_x = x - centroid_x
                away_y = y - centroid_y
                away_len = max(1e-6, math.hypot(away_x, away_y))

                move_x = (away_x / away_len) * 70.0 + (zx - x) * 0.75
                move_y = (away_y / away_len) * 70.0 + (zy - y) * 0.75
            else:
                move_x = zx - x
                move_y = zy - y

            move_reason = "threat_avoidance"

        elif target is not None:
            # Engage cautiously: maintain range and strafe.
            ex, ey, _ = target["pos"]
            edist = math.hypot(ex - x, ey - y)
            to_enemy_yaw = math.atan2(ey - y, ex - x)

            if self.rng.random() < 0.03 or t > self._strafe_until:
                self._strafe_dir = self.rng.choice([-1.0, 1.0])
                self._strafe_until = t + self.rng.uniform(1.5, 3.5)

            strafe_yaw = to_enemy_yaw + self._strafe_dir * (math.pi * 0.5)

            desired_range = 70.0 if hp_ratio > 0.65 else 110.0

            radial_pull = 0.0
            if edist < desired_range * 0.75:
                radial_pull = -0.70
            elif edist > desired_range * 1.30 and in_zone:
                radial_pull = 0.35

            blend_x = (
                (zx - x) * 0.45
                + math.cos(to_enemy_yaw) * radial_pull * 40.0
                + math.cos(strafe_yaw) * 28.0
            )
            blend_y = (
                (zy - y) * 0.45
                + math.sin(to_enemy_yaw) * radial_pull * 40.0
                + math.sin(strafe_yaw) * 28.0
            )

            move_x, move_y = blend_x, blend_y
            move_reason = "cautious_engagement"

        elif loot_target is not None:
            move_x = float(loot_target["pos"][0]) - x
            move_y = float(loot_target["pos"][1]) - y
            move_reason = "safe_loot"

        move_norm = math.hypot(move_x, move_y)

        if move_norm > 1e-6:
            speed_scale = 1.0

            if very_low_hp and not recently_hit and in_zone and len(hostile_mechs) == 0:
                speed_scale = 0.55

            act["move"] = [
                (move_x / move_norm) * speed_scale,
                (move_y / move_norm) * speed_scale,
            ]
        else:
            act["move"] = [0.0, 0.0]

        should_boost = (
            (not in_zone and energy > 20.0)
            or (my_zone_distance >= zone_radius * 0.90 and energy > 25.0)
            or (recently_hit and low_hp and energy > 35.0)
            or (loot_target is not None and loot_dist > 30.0 and energy > 45.0)
        )

        act["boost"] = bool(should_boost)
        act["jump"] = bool(
            me.get("grounded", False)
            and self.rng.random() < 0.015
            and len(hostile_mechs) > 0
            and total_threat < 0.65
        )

        if target is not None:
            tpos = target["pos"]
            tvel = target.get("vel", [0.0, 0.0, 0.0])
            horiz_dist = max(1.0, math.hypot(tpos[0] - x, tpos[1] - y))

            lead_t = min(0.9, horiz_dist / bullet_speed)
            px = tpos[0] + tvel[0] * lead_t
            py = tpos[1] + tvel[1] * lead_t

            t_cubes = int(target.get("cubes", 0))
            pz = tpos[2] + 4.5 * (1.0 + 0.05 * min(t_cubes, 10))

            yaw = math.atan2(py - y, px - x)
            pitch = math.atan2(pz - muzzle_z, horiz_dist)
            pitch = max(-0.8, min(0.8, pitch))

            act["aim"] = [yaw, pitch]

            target_weapons = target.get("weapons", [])
            target_has_mortar = "mortar" in target_weapons

            f0 = self._weapon_fire_decision(
                0,
                weapons[0] if len(weapons) > 0 else None,
                horiz_dist,
                True,
                energy,
                target_has_mortar,
            )
            f1 = self._weapon_fire_decision(
                1,
                weapons[1] if len(weapons) > 1 else None,
                horiz_dist,
                True,
                energy,
                target_has_mortar,
            )

            fire = [f0, f1]

            my_aim = me.get("aim", [yaw, pitch])
            aim_err = abs(_wrap_angle(float(my_aim[0]) - yaw))

            for i, w in enumerate(weapons[:2]):
                if w is not None and w.get("kind") == "laser" and self.laser_charging[i]:
                    continue
                if aim_err > 0.05:
                    fire[i] = False

            # Do not waste ammo when we are trying to disengage.
            if move_reason in ("low_hp_retreat", "threat_avoidance", "emergency_zone_escape"):
                if horiz_dist > 90.0:
                    fire = [False, False]

            act["fire"] = fire
            mf.draw.line([x, y, muzzle_z], [px, py, pz], "#ff5040")

        else:
            self.laser_charging = [False, False]

            if recently_hit and self.last_damage_bearing is not None:
                act["aim"] = [_wrap_angle(self.last_damage_bearing), 0.0]
            elif loot_target is not None:
                act["aim"] = [
                    math.atan2(float(loot_target["pos"][1]) - y, float(loot_target["pos"][0]) - x),
                    0.0,
                ]
            else:
                act["aim"] = [math.atan2(zy - y, zx - x), 0.0]

            act["fire"] = [False, False]

        mf.draw.point([zx, zy, z], "#40c0ff", 6)
        mf.draw.label(
            f"v5 hp {hp:.0f}/{max_hp:.0f} "
            f"threat {total_threat:.2f} "
            f"enemies {len(hostile_mechs)} "
            f"mode {move_reason}"
        )

        return act


mf.run(Bot)