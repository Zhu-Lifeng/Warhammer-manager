"""
build_kb.py — Reconstructs the wh40k knowledge base from BattleScribe .cat/.gst files.

Pipeline:  source *.cat/*.gst  ->  kb/json/*  ->  kb/wh40k.db

Usage:
    python build_kb.py <source_dir> [--out-json kb/json] [--out-db kb/wh40k.db]

The parser was reverse-engineered to reproduce the existing kb/json schema exactly
(validated against the original source files embedded in the old wh40k.db).
"""
import os, sys, json, glob, gzip, hashlib, sqlite3, argparse, datetime
import xml.etree.ElementTree as ET

# ---- BattleScribe profileType ids (stable across the game system) ----
TID_UNIT     = "c547-1836-d8a-ff4f"     # M T SV W LD OC
TID_RANGED   = "f77d-b953-8fa4-b762"     # Range A BS S AP D Keywords
TID_MELEE    = "8a40-4aaa-c780-9046"     # Range A WS S AP D Keywords
TID_ABILITY  = "9cc3-6d83-4dd3-9b64"     # Description
TID_TRANSPORT= "74f8-5443-9d6d-1f1e"     # Capacity
PTS_NAME     = "pts"

COND_SYMBOL = {
    "greaterThan": ">", "lessThan": "<", "atLeast": "≥",
    "atMost": "≤", "equalTo": "=", "notEqualTo": "≠",
}


def ln(tag):
    return tag.split('}')[-1]


def chars(profile):
    """Return {characteristic_name: text} for a <profile> element."""
    out = {}
    for cs in profile:
        if ln(cs.tag) != 'characteristics':
            continue
        for c in cs:
            if ln(c.tag) == 'characteristic':
                out[c.attrib.get('name')] = (c.text or '').strip()
    return out


def child(elem, container):
    """Yield child elements inside the named container (e.g. 'profiles')."""
    for c in elem:
        if ln(c.tag) == container:
            for x in c:
                yield x


class KB:
    def __init__(self, src_dir):
        self.src_dir = src_dir
        self.files = sorted(
            f for f in os.listdir(src_dir)
            if f.lower().endswith('.cat') or f.lower().endswith('.gst')
        )
        self.trees = {}          # filename -> root element
        self.idx_entry = {}      # id -> selectionEntry / selectionEntryGroup element
        self.idx_profile = {}    # id -> profile element
        self.idx_rule = {}       # id -> rule element
        self.ptype_name = {}     # profileType id -> name (across all files)
        self._load()

    def ability_type(self, type_id):
        """Profile-type name if this profile is an ability (not weapon/unit/
        transport), else None. Datasheets carry many ability profile types
        (Abilities, auras, faction-specific) — all but the structural ones count.
        """
        if type_id in (TID_UNIT, TID_RANGED, TID_MELEE, TID_TRANSPORT):
            return None
        return self.ptype_name.get(type_id, 'Abilities')

    def _load(self):
        for fn in self.files:
            root = ET.parse(os.path.join(self.src_dir, fn)).getroot()
            self.trees[fn] = root
            for e in root.iter():
                t = ln(e.tag)
                _id = e.attrib.get('id')
                if not _id:
                    continue
                if t in ('selectionEntry', 'selectionEntryGroup'):
                    self.idx_entry.setdefault(_id, e)
                elif t == 'profile':
                    self.idx_profile.setdefault(_id, e)
                elif t == 'rule':
                    self.idx_rule.setdefault(_id, e)
                elif t == 'profileType':
                    self.ptype_name.setdefault(_id, e.attrib.get('name'))

    # ---------- link resolution ----------
    def resolve_entry(self, link):
        """Resolve an <entryLink> to its target selectionEntry/Group element."""
        return self.idx_entry.get(link.attrib.get('targetId'))

    def resolve_profile(self, link):
        return self.idx_profile.get(link.attrib.get('targetId'))

    def effective_profiles(self, entry):
        """All <profile> elements on an entry: inline + infoLink(type=profile)."""
        out = list(child(entry, 'profiles'))
        for il in child(entry, 'infoLinks'):
            if il.attrib.get('type') == 'profile':
                p = self.resolve_profile(il)
                if p is not None:
                    out.append(p)
        return out

    def effective_children(self, entry):
        """Direct child selectionEntries + groups, with entryLinks resolved,
        in document order.

        Returns list of (element, link_id) where link_id is the entryLink id when
        the element came in through a link (used for slot ids), else None.
        entryLinks may target either a selectionEntry or a selectionEntryGroup.
        """
        out = []
        for cont in entry:
            tag = ln(cont.tag)
            if tag in ('selectionEntries', 'selectionEntryGroups'):
                for x in cont:
                    out.append((x, None))
            elif tag == 'entryLinks':
                for el in cont:
                    tgt = self.resolve_entry(el)
                    if tgt is not None:
                        out.append((tgt, el.attrib.get('id')))
        return out

    def loadout_children(self, entry):
        """Children for slot ordering: direct entries, then groups, then links.

        This matches the order datasheet loadouts are laid out in: fixed wargear
        entries first, then choice groups, then linked wargear.
        """
        entries, groups, links = [], [], []
        for cont in entry:
            tag = ln(cont.tag)
            if tag == 'selectionEntries':
                for se in cont:
                    entries.append((se, None))
            elif tag == 'selectionEntryGroups':
                for g in cont:
                    groups.append((g, None))
            elif tag == 'entryLinks':
                for el in cont:
                    tgt = self.resolve_entry(el)
                    if tgt is not None:
                        links.append((tgt, el.attrib.get('id')))
        return entries + groups + links


# ---------------- weapon / ability / stat extraction ----------------
def parse_weapon(profile):
    tid = profile.attrib.get('typeId')
    if tid == TID_RANGED:
        wtype, bsws_key = 'Ranged', 'BS'
    elif tid == TID_MELEE:
        wtype, bsws_key = 'Melee', 'WS'
    else:
        return None
    ch = chars(profile)
    return {
        "profile_id": profile.attrib.get('id'),
        "name": profile.attrib.get('name'),
        "weapon_type": wtype,
        "range": ch.get('Range', ''),
        "a": ch.get('A', ''),
        "bs_ws": ch.get(bsws_key, ''),
        "s": ch.get('S', ''),
        "ap": ch.get('AP', ''),
        "d": ch.get('D', ''),
        "keywords": ch.get('Keywords', ''),
    }


def parse_ability(profile):
    if profile.attrib.get('typeId') != TID_ABILITY:
        return None
    ch = chars(profile)
    return {
        "id": profile.attrib.get('id'),
        "name": profile.attrib.get('name'),
        "type": "Abilities",
        "description": ch.get('Description', ''),
    }


def stats_of(entry, kb):
    for p in kb.effective_profiles(entry):
        if p.attrib.get('typeId') == TID_UNIT:
            ch = chars(p)
            return {
                "m": ch.get('M', ''), "t": ch.get('T', ''),
                "sv": ch.get('SV', ''), "w": ch.get('W', ''),
                "ld": ch.get('LD', ''), "oc": ch.get('OC', ''),
            }
    return None


def weapons_in(entry, kb):
    """All weapon profiles reachable from an entry (inline + infoLink profiles)."""
    out = []
    for p in kb.effective_profiles(entry):
        w = parse_weapon(p)
        if w:
            out.append(w)
    return out


def constraint_minmax(entry):
    """Return (min, max) from selections constraints scope=parent, default (1,1).

    Only parent-scoped constraints describe squad composition; roster/force
    scoped constraints (army-building limits) are ignored.
    """
    mn = mx = None
    for c in child(entry, 'constraints'):
        if c.attrib.get('field') != 'selections' or c.attrib.get('scope') != 'parent':
            continue
        try:
            v = int(c.attrib.get('value'))
        except (TypeError, ValueError):
            continue
        if c.attrib.get('type') == 'min':
            mn = v
        elif c.attrib.get('type') == 'max':
            mx = v
    if mn is None:
        mn = 1
    if mx is None:
        mx = mn
    return mn, mx


# ---------------- models & loadout ----------------
def inline_unit_profile(entry):
    for p in child(entry, 'profiles'):
        if p.attrib.get('typeId') == TID_UNIT:
            return p
    return None


def is_carrier(elem):
    """A 'model' is any selectionEntry that carries a Unit stat profile."""
    return ln(elem.tag) == 'selectionEntry' and inline_unit_profile(elem) is not None


def slot_minmax(elem):
    """min/max for a loadout slot: parent-scoped constraints, default (None, None)."""
    mn = mx = None
    for c in child(elem, 'constraints'):
        if c.attrib.get('field') != 'selections' or c.attrib.get('scope') != 'parent':
            continue
        try:
            v = int(c.attrib.get('value'))
        except (TypeError, ValueError):
            continue
        if c.attrib.get('type') == 'min':
            mn = v
        elif c.attrib.get('type') == 'max':
            mx = v
    return mn, mx


def flatten_weapons(entry, kb, seen=None, visited=None):
    """All weapon profiles in an entry's subtree, document order, links resolved."""
    if seen is None:
        seen, visited = set(), set()
    eid = entry.attrib.get('id')
    if eid in visited:
        return []
    visited.add(eid)
    out = []

    def add_weapon(p):
        w = parse_weapon(p)
        if w and w['profile_id'] not in seen:
            seen.add(w['profile_id'])
            out.append(w)

    for cont in entry:
        tag = ln(cont.tag)
        if tag == 'profiles':
            for p in cont:
                add_weapon(p)
        elif tag == 'infoLinks':
            for il in cont:
                if il.attrib.get('type') == 'profile':
                    p = kb.resolve_profile(il)
                    if p is not None:
                        add_weapon(p)
        elif tag in ('selectionEntries', 'selectionEntryGroups'):
            for sub in cont:
                out += flatten_weapons(sub, kb, seen, visited)
        elif tag == 'entryLinks':
            # follow links to shared weapon entries (the 10e data routes much
            # wargear through links; the visited set guards against cycles)
            for el in cont:
                tgt = kb.resolve_entry(el)
                if tgt is not None:
                    out += flatten_weapons(tgt, kb, seen, visited)
    return out


def build_loadout(model_entry, kb):
    """Loadout slots for a model (carrier) entry.

    A child weapon-bearing entry -> fixed slot; a group of non-carrier options
    -> choice slot. Groups/entries that are themselves models are skipped (they
    are separate models, not loadout). Option weapons are flattened recursively.
    """
    slots = []
    _collect_slots(model_entry, kb, slots, set())
    return slots


def _collect_slots(container, kb, slots, visited):
    """Append loadout slots from a model or a wrapper group.

    Wrapper groups (whose children are sub-groups, e.g. 'Wargear') are descended
    into so their leaf choices become slots. A weapon-bearing entry is a fixed
    slot; a group of non-carrier options is a choice slot.
    """
    cid = container.attrib.get('id')
    if cid in visited:
        return
    visited.add(cid)
    for elem, link_id in kb.loadout_children(container):
        t = ln(elem.tag)
        sid = link_id or elem.attrib.get('id')
        if t == 'selectionEntry':
            if is_carrier(elem):
                continue
            ws = flatten_weapons(elem, kb)
            if not ws:
                continue
            mn, mx = slot_minmax(elem)
            slots.append({
                "id": sid, "kind": "fixed", "name": elem.attrib.get('name'),
                "min_select": mn, "max_select": mx,
                "options": [{
                    "id": f"{sid}:{elem.attrib.get('id')}",
                    "name": elem.attrib.get('name'),
                    "is_default": True, "weapons": ws,
                }],
            })
        elif t == 'selectionEntryGroup':
            # options are the group's selectionEntries, including those reached
            # through entryLinks (10e routes many weapon options through links).
            kids = kb.effective_children(elem)
            opt_entries = [(o, olink) for o, olink in kids
                           if ln(o.tag) == 'selectionEntry']
            if any(is_carrier(o) for o, _ in opt_entries):
                continue  # model-composition group -> handled as separate models
            # A group that also contains sub-groups is a wrapper (e.g. 'Wargear'
            # holding fixed items + 'Weapon Option N' choices): descend so each
            # leaf becomes its own slot rather than collapsing to one choice.
            if any(ln(o.tag) == 'selectionEntryGroup' for o, _ in kids):
                _collect_slots(elem, kb, slots, visited)
                continue
            default_id = elem.attrib.get('defaultSelectionEntryId')
            options = []
            for o, olink in opt_entries:
                ws = flatten_weapons(o, kb)
                if not ws:
                    continue
                oid = olink or o.attrib.get('id')
                options.append({
                    "id": f"{sid}:{oid}", "name": o.attrib.get('name'),
                    "is_default": (oid == default_id), "weapons": ws,
                })
            if options:
                mn, mx = slot_minmax(elem)
                slots.append({
                    "id": sid, "kind": "choice", "name": elem.attrib.get('name'),
                    "min_select": mn, "max_select": mx, "options": options,
                })
            else:
                # wrapper group: descend so its sub-groups become slots
                _collect_slots(elem, kb, slots, visited)


def collect_models(ds_entry, kb):
    """All carrier entries in the datasheet subtree, in document order."""
    return [e for e in ds_entry.iter() if is_carrier(e)]


def parse_model(entry, kb, is_ds_level):
    if is_ds_level:
        mn, mx = 1, 1                 # the datasheet's representative model
    else:
        mn, mx = slot_minmax(entry)   # nested sub-model: parent constraints, else None
    p = inline_unit_profile(entry)
    ch = chars(p) if p is not None else {}
    return {
        "id": entry.attrib.get('id'),
        "name": entry.attrib.get('name'),
        "min_count": mn,
        "max_count": mx,
        "stats": {
            "m": ch.get('M', ''), "t": ch.get('T', ''), "sv": ch.get('SV', ''),
            "w": ch.get('W', ''), "ld": ch.get('LD', ''), "oc": ch.get('OC', ''),
        },
        "loadout": build_loadout(entry, kb),
    }


# ---------------- abilities / transport / keywords / points ----------------
def datasheet_abilities(ds_entry, kb):
    """Inline Abilities-type profiles found anywhere in the datasheet subtree.

    infoLink profiles (e.g. Flip Belt) are excluded — only inline <profile>
    elements are collected, in document order, deduped by profile id. Hidden
    profiles are kept (e.g. aura abilities).
    """
    out, seen = [], set()
    for p in ds_entry.iter():
        if ln(p.tag) != 'profile':
            continue
        atype = kb.ability_type(p.attrib.get('typeId'))
        if atype is None:
            continue
        pid = p.attrib.get('id')
        if pid in seen:
            continue
        seen.add(pid)
        ch = chars(p)
        out.append({
            "id": pid, "name": p.attrib.get('name'),
            "type": atype, "description": ch.get('Description', ''),
        })
    return out


def datasheet_transport(ds_entry, kb):
    out = []
    for p in kb.effective_profiles(ds_entry):
        if p.attrib.get('typeId') == TID_TRANSPORT:
            ch = chars(p)
            out.append({
                "id": p.attrib.get('id'),
                "name": p.attrib.get('name'),
                "capacity": ch.get('Capacity', ''),
            })
    return out


def datasheet_keywords(ds_entry):
    out = []
    for cl in child(ds_entry, 'categoryLinks'):
        out.append({
            "name": cl.attrib.get('name'),
            "target_id": cl.attrib.get('targetId'),
            "primary": cl.attrib.get('primary') == 'true',
        })
    return out


def base_points(entry):
    for c in child(entry, 'costs'):
        if c.attrib.get('name') == PTS_NAME:
            try:
                return int(round(float(c.attrib.get('value'))))
            except (TypeError, ValueError):
                return None
    return None


def _pts_type_id(ds_entry):
    for c in child(ds_entry, 'costs'):
        if c.attrib.get('name') == PTS_NAME:
            return c.attrib.get('typeId')
    return "51b2-306e-1021-d207"


def _is_model_child(child_id, kb):
    """True if a condition's childId denotes a model count (vs a detachment).

    'model' literal, a model/unit entry, or a composition group all count;
    a detachment (an 'upgrade' entry) does not.
    """
    if child_id == 'model':
        return True
    tgt = kb.idx_entry.get(child_id)
    if tgt is None:
        return False
    if ln(tgt.tag) == 'selectionEntryGroup':
        return True
    return tgt.attrib.get('type') in ('model', 'unit')


def pricing_tiers(ds_entry, kb):
    """Tiers come from modifiers that set the pts cost based on unit size.

    A pts-set modifier is a size tier when its first condition counts models
    (childId='model' or a model/unit child). Modifiers conditioned on a
    detachment/roster are army-building rules, not size tiers, and are skipped.
    The first condition supplies the structured fields; condition_text renders
    every leaf condition (in English) joined by ∧.
    """
    pts_field = _pts_type_id(ds_entry)
    tiers = []
    for mod in child(ds_entry, 'modifiers'):
        if mod.attrib.get('type') != 'set' or mod.attrib.get('field') != pts_field:
            continue
        try:
            pts = int(round(float(mod.attrib.get('value'))))
        except (TypeError, ValueError):
            continue
        conds = [c for c in mod.iter()
                 if ln(c.tag) == 'condition' and c.attrib.get('field') == 'selections']
        if not conds:
            continue
        first = conds[0]
        if not _is_model_child(first.attrib.get('childId'), kb):
            continue

        def render(c):
            sym = COND_SYMBOL.get(c.attrib.get('type'), c.attrib.get('type'))
            cid = c.attrib.get('childId')
            if cid == 'model':
                unit = 'models'
            else:
                tgt = kb.idx_entry.get(cid)
                unit = tgt.attrib.get('name') if tgt is not None else 'models'
            return f"{sym} {c.attrib.get('value')} {unit}"

        cid = first.attrib.get('childId')
        if cid == 'model':
            target_name, scope_id = '', 'model'
        else:
            tgt = kb.idx_entry.get(cid)
            target_name = tgt.attrib.get('name') if tgt is not None else ''
            scope_id = cid or ''
        tiers.append({
            "points": pts,
            "condition_text": " ∧ ".join(render(c) for c in conds),
            "condition_type": first.attrib.get('type'),
            "condition_value": int(first.attrib.get('value')),
            "target_name": target_name,
            "scope_id": scope_id,
        })
    return tiers


# ---------------- datasheet discovery ----------------
def find_datasheets(root):
    """All selectionEntries of type model/unit with no model/unit ancestor."""
    parents = {c: p for p in root.iter() for c in p}
    result = []
    for e in root.iter():
        if ln(e.tag) != 'selectionEntry':
            continue
        if e.attrib.get('type') not in ('model', 'unit'):
            continue
        x, is_ds = e, True
        while x in parents:
            x = parents[x]
            if ln(x.tag) == 'selectionEntry' and x.attrib.get('type') in ('model', 'unit'):
                is_ds = False
                break
        if is_ds:
            result.append(e)
    return result


# ---------------- detachments / enhancements / categories / rules ----------------
def find_detachments(root):
    """selectionEntries directly under a group named 'Detachment'/'Detachments'."""
    out = []
    for g in root.iter():
        if ln(g.tag) == 'selectionEntryGroup' and \
                g.attrib.get('name') in ('Detachment', 'Detachments'):
            for se in child(g, 'selectionEntries'):
                out.append({"id": se.attrib.get('id'), "name": se.attrib.get('name')})
    return out


def find_enhancements(root, kb):
    """selectionEntries inside a group named 'Enhancements'."""
    out = []

    def collect_group(grp):
        for se in child(grp, 'selectionEntries'):
            desc = ''
            for p in kb.effective_profiles(se):
                if p.attrib.get('typeId') == TID_ABILITY:
                    desc = chars(p).get('Description', '')
                    break
            out.append({
                "id": se.attrib.get('id'),
                "name": se.attrib.get('name'),
                "points": base_points(se),
                "description": desc,
            })

    for e in root.iter():
        if ln(e.tag) == 'selectionEntryGroup' and e.attrib.get('name') == 'Enhancements':
            collect_group(e)
    return out


def find_categories(root):
    out = []
    for cont in root:
        if ln(cont.tag) == 'categoryEntries':
            for ce in cont:
                if ln(ce.tag) == 'categoryEntry':
                    out.append({"id": ce.attrib.get('id'), "name": ce.attrib.get('name')})
    return out


def find_rules(root):
    # The KB's per-catalogue 'rules' list is unused downstream and was always
    # empty in the original data; kept as an empty list for schema parity.
    return []


# ---------------- per-catalogue parse ----------------
def parse_catalogue(fn, root, kb):
    cat_id = root.attrib.get('id')
    name = root.attrib.get('name')
    is_library = root.attrib.get('library') == 'true'
    revision = root.attrib.get('revision')

    datasheets = []
    for ds in find_datasheets(root):
        ds_id = ds.attrib.get('id')
        models = [parse_model(m, kb, m.attrib.get('id') == ds_id)
                  for m in collect_models(ds, kb)]
        datasheets.append({
            "id": ds.attrib.get('id'),
            "name": ds.attrib.get('name'),
            "type": ds.attrib.get('type'),
            "points": base_points(ds),
            "keywords": datasheet_keywords(ds),
            "models": models,
            "abilities": datasheet_abilities(ds, kb),
            "transport": datasheet_transport(ds, kb),
            "pricing_tiers": pricing_tiers(ds, kb),
        })

    return {
        "id": cat_id,
        "name": name,
        "is_library": is_library,
        "revision": revision,
        "source_file": fn,
        "categories": find_categories(root),
        "rules": find_rules(root),
        "datasheets": datasheets,
        "detachments": find_detachments(root),
        "enhancements": find_enhancements(root, kb),
    }


def safe_name(fn):
    import re
    return re.sub(r'[^A-Za-z0-9]+', '_', os.path.splitext(fn)[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src_dir')
    ap.add_argument('--out-json', default='kb/json')
    ap.add_argument('--out-db', default='kb/wh40k.db')
    ap.add_argument('--validate-against', default=None,
                    help='existing kb/json dir to diff against (no writes)')
    args = ap.parse_args()

    kb = KB(args.src_dir)
    print(f"Loaded {len(kb.files)} files; "
          f"{len(kb.idx_entry)} entries, {len(kb.idx_profile)} profiles, "
          f"{len(kb.idx_rule)} rules indexed.")

    catalogues = []
    per_cat = {}
    for fn in kb.files:
        # the game system (.gst) is recorded as a catalogue too (it owns shared
        # categories/rules) but contributes no datasheets.
        c = parse_catalogue(fn, kb.trees[fn], kb)
        per_cat[fn] = c
        catalogues.append(c)
        print(f"  {fn}: {len(c['datasheets'])} datasheets, "
              f"{len(c['detachments'])} det, {len(c['enhancements'])} enh")

    if args.validate_against:
        validate(per_cat, args.validate_against)
        return

    write_json(per_cat, catalogues, args.out_json)
    write_db(per_cat, kb, args.out_db, args.src_dir)


def write_json(per_cat, catalogues, out_dir):
    os.makedirs(os.path.join(out_dir, 'by_catalogue'), exist_ok=True)
    cat_index = []
    all_ds, all_w, all_a = [], [], []
    all_a_ids = set()
    for c in catalogues:
        jf = f"by_catalogue/{safe_name(c['source_file'])}.json"
        with open(os.path.join(out_dir, jf), 'w', encoding='utf-8') as f:
            json.dump(c, f, ensure_ascii=False, indent=1)
        cat_index.append({
            "id": c['id'], "name": c['name'], "is_library": c['is_library'],
            "revision": c['revision'], "source_file": c['source_file'],
            "datasheet_count": len(c['datasheets']), "json_file": jf,
        })
        for d in c['datasheets']:
            all_ds.append({"catalogue": c['name'], **d})
            seen_w = set()                      # one weapon row per datasheet
            for m in d['models']:
                for s in m['loadout']:
                    for o in s['options']:
                        for w in o['weapons']:
                            if w['profile_id'] in seen_w:
                                continue
                            seen_w.add(w['profile_id'])
                            all_w.append({
                                "catalogue": c['name'], "datasheet": d['name'],
                                "id": w['profile_id'], "name": w['name'],
                                "weapon_type": w['weapon_type'], "range": w['range'],
                                "A": w['a'], "BS/WS": w['bs_ws'], "S": w['s'],
                                "AP": w['ap'], "D": w['d'], "keywords": w['keywords'],
                            })
            for a in d['abilities']:
                if a['id'] in all_a_ids:         # one ability row per profile id
                    continue
                all_a_ids.add(a['id'])
                all_a.append({"catalogue": c['name'], "datasheet": d['name'], **a})
    for fname, data in [('catalogues.json', cat_index),
                        ('all_datasheets.json', all_ds),
                        ('all_weapons.json', all_w),
                        ('all_abilities.json', all_a)]:
        with open(os.path.join(out_dir, fname), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"Wrote JSON: {len(all_ds)} datasheets, {len(all_w)} weapons, "
          f"{len(all_a)} abilities.")


SCHEMA = """
CREATE TABLE catalogues (id TEXT PRIMARY KEY, name TEXT, is_library INTEGER,
    revision TEXT, source_file TEXT);
CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT, catalogue_id TEXT);
CREATE TABLE datasheets (id TEXT PRIMARY KEY, name TEXT, entry_type TEXT,
    points INTEGER, catalogue_id TEXT, source_file TEXT);
CREATE TABLE datasheet_keywords (datasheet_id TEXT, category_id TEXT,
    category_name TEXT, is_primary INTEGER,
    PRIMARY KEY (datasheet_id, category_id, category_name));
CREATE TABLE unit_models (id TEXT PRIMARY KEY, datasheet_id TEXT NOT NULL,
    name TEXT, min_count INTEGER, max_count INTEGER, sort_order INTEGER,
    m TEXT, t TEXT, sv TEXT, w TEXT, ld TEXT, oc TEXT);
CREATE TABLE weapons (profile_id TEXT PRIMARY KEY, datasheet_id TEXT, name TEXT,
    weapon_type TEXT, range_ TEXT, a TEXT, bs_ws TEXT, s TEXT, ap TEXT, d TEXT,
    keywords TEXT, catalogue_id TEXT, source_file TEXT);
CREATE TABLE abilities (profile_id TEXT PRIMARY KEY, datasheet_id TEXT, name TEXT,
    ability_type TEXT, description TEXT, catalogue_id TEXT, source_file TEXT);
CREATE TABLE transport (profile_id TEXT PRIMARY KEY, datasheet_id TEXT, capacity TEXT);
CREATE TABLE pricing_tiers (id INTEGER PRIMARY KEY AUTOINCREMENT,
    datasheet_id TEXT NOT NULL, points INTEGER, condition_text TEXT,
    condition_type TEXT, condition_value INTEGER, target_name TEXT, scope_id TEXT);
CREATE TABLE loadout_slots (id TEXT PRIMARY KEY, model_id TEXT NOT NULL,
    slot_name TEXT, kind TEXT, min_select INTEGER, max_select INTEGER, sort_order INTEGER);
CREATE TABLE loadout_options (id TEXT PRIMARY KEY, slot_id TEXT NOT NULL, name TEXT,
    is_default INTEGER, sort_order INTEGER);
CREATE TABLE loadout_option_weapons (option_id TEXT NOT NULL,
    weapon_profile_id TEXT NOT NULL, sort_order INTEGER,
    PRIMARY KEY (option_id, weapon_profile_id));
CREATE TABLE detachments (id TEXT PRIMARY KEY, name TEXT, catalogue_id TEXT, source_file TEXT);
CREATE TABLE enhancements (id TEXT PRIMARY KEY, name TEXT, description TEXT,
    points INTEGER, catalogue_id TEXT, detachment_id TEXT, source_file TEXT);
CREATE TABLE rules (id TEXT PRIMARY KEY, name TEXT, description TEXT,
    catalogue_id TEXT, source_file TEXT);
CREATE TABLE source_files (filename TEXT PRIMARY KEY, size_bytes INTEGER,
    sha256 TEXT, embedded_at TEXT, content_gz BLOB NOT NULL);
CREATE INDEX idx_datasheets_cat    ON datasheets(catalogue_id);
CREATE INDEX idx_unit_models_ds    ON unit_models(datasheet_id);
CREATE INDEX idx_weapons_ds        ON weapons(datasheet_id);
CREATE INDEX idx_abilities_ds      ON abilities(datasheet_id);
CREATE INDEX idx_ds_keywords_ds    ON datasheet_keywords(datasheet_id);
CREATE INDEX idx_pricing_tiers_ds  ON pricing_tiers(datasheet_id);
CREATE INDEX idx_loadout_slots_model  ON loadout_slots(model_id);
CREATE INDEX idx_loadout_options_slot ON loadout_options(slot_id);
CREATE INDEX idx_low_option           ON loadout_option_weapons(option_id);
"""


def find_rule_rows(root, cat_id, fn):
    """Every <rule> element in a file (shared rules + detachment rules)."""
    rows = []
    for r in root.iter():
        if ln(r.tag) == 'rule':
            desc = ''
            for d in r:
                if ln(d.tag) == 'description':
                    desc = (d.text or '').strip()
            rows.append((r.attrib.get('id'), r.attrib.get('name'), desc, cat_id, fn))
    return rows


def write_db(per_cat, kb, out_db, src_dir):
    if os.path.exists(out_db):
        os.remove(out_db)
    conn = sqlite3.connect(out_db)
    conn.executescript(SCHEMA)
    now = datetime.datetime.utcnow().isoformat()

    for c in per_cat.values():
        cid, fn = c['id'], c['source_file']
        is_lib = 1 if c['is_library'] else 0
        conn.execute("INSERT OR IGNORE INTO catalogues VALUES (?,?,?,?,?)",
                     (cid, c['name'], is_lib, c['revision'], fn))
        conn.executemany("INSERT OR IGNORE INTO categories VALUES (?,?,?)",
                         [(cat['id'], cat['name'], cid) for cat in c['categories']])
        conn.executemany("INSERT OR IGNORE INTO detachments VALUES (?,?,?,?)",
                         [(d['id'], d['name'], cid, fn) for d in c['detachments']])
        conn.executemany("INSERT OR IGNORE INTO enhancements VALUES (?,?,?,?,?,?,?)",
                         [(e['id'], e['name'], e['description'], e['points'], cid, None, fn)
                          for e in c['enhancements']])
        for d in c['datasheets']:
            did = d['id']
            conn.execute("INSERT OR IGNORE INTO datasheets VALUES (?,?,?,?,?,?)",
                         (did, d['name'], d['type'], d['points'], cid, fn))
            conn.executemany(
                "INSERT OR IGNORE INTO datasheet_keywords VALUES (?,?,?,?)",
                [(did, k['target_id'], k['name'], 1 if k['primary'] else 0)
                 for k in d['keywords']])
            for ab in d['abilities']:
                conn.execute("INSERT OR IGNORE INTO abilities VALUES (?,?,?,?,?,?,?)",
                             (ab['id'], did, ab['name'], ab['type'], ab['description'], cid, fn))
            for tr in d['transport']:
                conn.execute("INSERT OR IGNORE INTO transport VALUES (?,?,?)",
                             (tr['id'], did, tr['capacity']))
            for t in d['pricing_tiers']:
                conn.execute(
                    "INSERT INTO pricing_tiers "
                    "(datasheet_id,points,condition_text,condition_type,"
                    "condition_value,target_name,scope_id) VALUES (?,?,?,?,?,?,?)",
                    (did, t['points'], t['condition_text'], t['condition_type'],
                     t['condition_value'], t['target_name'], t['scope_id']))
            for mi, m in enumerate(d['models']):
                st = m['stats']
                conn.execute(
                    "INSERT OR IGNORE INTO unit_models VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (m['id'], did, m['name'], m['min_count'], m['max_count'], mi,
                     st.get('m'), st.get('t'), st.get('sv'), st.get('w'),
                     st.get('ld'), st.get('oc')))
                for si, s in enumerate(m['loadout']):
                    conn.execute(
                        "INSERT OR IGNORE INTO loadout_slots VALUES (?,?,?,?,?,?,?)",
                        (s['id'], m['id'], s['name'], s['kind'],
                         s['min_select'], s['max_select'], si))
                    for oi, o in enumerate(s['options']):
                        conn.execute(
                            "INSERT OR IGNORE INTO loadout_options VALUES (?,?,?,?,?)",
                            (o['id'], s['id'], o['name'], 1 if o['is_default'] else 0, oi))
                        for wi, w in enumerate(o['weapons']):
                            conn.execute(
                                "INSERT OR IGNORE INTO loadout_option_weapons VALUES (?,?,?)",
                                (o['id'], w['profile_id'], wi))
                            conn.execute(
                                "INSERT OR IGNORE INTO weapons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (w['profile_id'], did, w['name'], w['weapon_type'],
                                 w['range'], w['a'], w['bs_ws'], w['s'], w['ap'],
                                 w['d'], w['keywords'], cid, fn))

    # rules: every <rule> element across catalogues + the game system file
    for fn, root in kb.trees.items():
        cat_id = root.attrib.get('id')
        conn.executemany("INSERT OR IGNORE INTO rules VALUES (?,?,?,?,?)",
                         find_rule_rows(root, cat_id, fn))

    # source_files: embed the gzipped originals with checksums
    for fn in kb.files:
        data = open(os.path.join(src_dir, fn), 'rb').read()
        conn.execute("INSERT OR REPLACE INTO source_files VALUES (?,?,?,?,?)",
                     (fn, len(data), hashlib.sha256(data).hexdigest(), now,
                      gzip.compress(data)))

    conn.commit()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
              ('catalogues', 'datasheets', 'unit_models', 'weapons', 'abilities',
               'loadout_slots', 'loadout_options', 'loadout_option_weapons',
               'pricing_tiers', 'detachments', 'enhancements', 'rules',
               'transport', 'categories', 'datasheet_keywords', 'source_files')}
    conn.close()
    print("Wrote DB:", out_db)
    for k, v in counts.items():
        print(f"    {k}: {v}")


# ---------------- validation ----------------
def validate(per_cat, gold_dir):
    import collections
    mismatch = collections.Counter()
    examples = collections.defaultdict(list)
    for fn, c in per_cat.items():
        gf = os.path.join(gold_dir, 'by_catalogue', f"{safe_name(fn)}.json")
        if not os.path.exists(gf):
            print("NO GOLD:", fn); continue
        gold = json.load(open(gf, encoding='utf-8'))
        gd = {d['id']: d for d in gold['datasheets']}
        nd = {d['id']: d for d in c['datasheets']}
        if set(gd) != set(nd):
            mismatch['datasheet_set'] += 1
            examples['datasheet_set'].append((fn, sorted(set(gd)-set(nd))[:3], sorted(set(nd)-set(gd))[:3]))
        for did in set(gd) & set(nd):
            g, n = gd[did], nd[did]
            for key in ('name', 'type', 'points'):
                if g.get(key) != n.get(key):
                    mismatch[f'ds.{key}'] += 1
                    examples[f'ds.{key}'].append((c['name'], g['name'], g.get(key), n.get(key)))
            if json.dumps(g.get('keywords'), sort_keys=True) != json.dumps(n.get('keywords'), sort_keys=True):
                mismatch['ds.keywords'] += 1
                examples['ds.keywords'].append((c['name'], g['name']))
            if json.dumps(g.get('abilities'), sort_keys=True) != json.dumps(n.get('abilities'), sort_keys=True):
                mismatch['ds.abilities'] += 1
                examples['ds.abilities'].append((c['name'], g['name']))
            if json.dumps(g.get('transport'), sort_keys=True) != json.dumps(n.get('transport'), sort_keys=True):
                mismatch['ds.transport'] += 1
            if json.dumps(g.get('models'), sort_keys=True) != json.dumps(n.get('models'), sort_keys=True):
                mismatch['ds.models'] += 1
                examples['ds.models'].append((c['name'], g['name']))
            if len(g.get('pricing_tiers', [])) != len(n.get('pricing_tiers', [])):
                mismatch['ds.tiers'] += 1
                examples['ds.tiers'].append((c['name'], g['name'], g.get('pricing_tiers'), n.get('pricing_tiers')))
        for fld in ('detachments', 'enhancements', 'categories', 'rules'):
            if len(gold.get(fld, [])) != len(c.get(fld, [])):
                mismatch[f'cat.{fld}'] += 1
                examples[f'cat.{fld}'].append((fn, len(gold.get(fld, [])), len(c.get(fld, []))))
    print("\n===== VALIDATION =====")
    if not mismatch:
        print("PERFECT MATCH")
    for k, v in sorted(mismatch.items(), key=lambda x: -x[1]):
        print(f"{k}: {v} mismatches")
        for ex in examples[k][:4]:
            print("     e.g.", ex)


if __name__ == '__main__':
    main()
