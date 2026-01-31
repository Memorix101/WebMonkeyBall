#!/usr/bin/env python3
"""Build an SMB2-style web pack from an extracted ROM folder.

US NTSC layout assumed. This tool extracts stage env data (bg + fog) and
copies only required stage/bg/init files into a pack folder (and optional zip).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

STAGE_WORLD_THEMES_LEN = 420
BG_NAME_COUNT = 43
THEME_LIGHT_COUNT = 41
KEYFRAME_SIZE = 0x14

# Default symbol addresses from mkb2.us.lst (NTSC SMB2).
DEFAULT_SYMBOLS = {
    'STAGE_WORLD_THEMES': 0x80474F48,
    'g_bg_filename_list': 0x80474D44,
    'theme_lights': 0x80455398,
}

# Default file offset for STAGE_WORLD_THEMES table in mkb2.main_loop.rel (NTSC SMB2).
DEFAULT_STAGE_WORLD_FILE_OFF = 0x204E48

# Hard-coded bg filename table (WorldTheme -> bg file), SMB2 NTSC.
BG_NAME_TABLE: List[Optional[str]] = [
    None,  # 0
    None,  # 1
    'bg_jun',
    'bg_wat',
    'bg_nig',
    'bg_sun',
    'bg_spa',
    'bg_snd',
    'bg_ice',
    'bg_stm',
    'bg_bns',
    'bg_pil',
    None,  # 12
    'bg_gol',
    'bg_bow',
    'bg_mst',
    'bg_ending',
    'bg_lav2',
    'bg_wat2',
    'bg_jun2',
    'bg_par2',
    'bg_pot2',
    'bg_spa2',
    'bg_ele2',
    'bg_gea2',
    'bg_bub2',
    'bg_bns2',
    'bg_fut2',
    'bg_bow2',
    'bg_tar2',
    None,  # 30
    None,  # 31
    'bg_wha2',
    'bg_gol2',
    'bg_pot2',
    'bg_vil2',
    'bg_au_bub2',
    'bg_au_par2',
    'bg_au_gea2',
    'bg_au_wat2',
    'bg_au_tar2',
    'bg_bow2',
    None,  # 42
]

# REL relocation constants (PowerPC REL format)
R_PPC_NONE = 0
R_PPC_SECTION = 202


@dataclass
class RelSection:
    offset: int
    size: int
    flags: int


@dataclass
class RelHeader:
    section_table_off: int
    section_count: int
    imp_off: int
    imp_size: int


@dataclass
class Relocation:
    patch_section: int
    patch_offset: int
    rel_type: int
    target_section: int
    addend: int


@dataclass
class FogAnim:
    start: Optional[List[Dict[str, float]]]
    end: Optional[List[Dict[str, float]]]
    r: Optional[List[Dict[str, float]]]
    g: Optional[List[Dict[str, float]]]
    b: Optional[List[Dict[str, float]]]


@dataclass
class StageFog:
    fog_type: int
    start: float
    end: float
    color: Tuple[float, float, float]
    anim: Optional[FogAnim]


def read_u32_be(data: bytes, offset: int) -> int:
    return struct.unpack_from('>I', data, offset)[0]


def read_s32_be(data: bytes, offset: int) -> int:
    return struct.unpack_from('>i', data, offset)[0]


def read_f32_be(data: bytes, offset: int) -> float:
    return struct.unpack_from('>f', data, offset)[0]


def read_u16_be(data: bytes, offset: int) -> int:
    return struct.unpack_from('>H', data, offset)[0]


def read_s16_be(data: bytes, offset: int) -> int:
    return struct.unpack_from('>h', data, offset)[0]


def lzss_decompress(buffer: bytes) -> bytes:
    if len(buffer) < 8:
        return b''
    src_size = struct.unpack_from('<I', buffer, 0)[0]
    dest_size = struct.unpack_from('<I', buffer, 4)[0]
    if src_size <= 8 or dest_size <= 0:
        return b''
    src = buffer[8:8 + (src_size - 8)]
    dest = bytearray(dest_size)
    ring = bytearray(4096)
    buf_pos = 4078
    flags = 0
    srcp = 0
    destp = 0
    while True:
        flags >>= 1
        if (flags & 0x100) == 0:
            if srcp >= len(src):
                break
            flags = src[srcp] | 0xFF00
            srcp += 1
        if flags & 1:
            if srcp >= len(src):
                break
            byte = src[srcp]
            srcp += 1
            if destp >= len(dest):
                break
            dest[destp] = byte
            ring[buf_pos] = byte
            buf_pos = (buf_pos + 1) & 4095
            destp += 1
        else:
            if srcp + 1 >= len(src):
                break
            offset = src[srcp]
            r8 = src[srcp + 1]
            srcp += 2
            length = (r8 & 0x0F) + 2
            offset |= (r8 & 0xF0) << 4
            for i in range(length + 1):
                byte = ring[(offset + i) & 4095]
                if destp >= len(dest):
                    break
                dest[destp] = byte
                ring[buf_pos] = byte
                buf_pos = (buf_pos + 1) & 4095
                destp += 1
    return bytes(dest)


def parse_rel_header(data: bytes) -> RelHeader:
    header = struct.unpack_from('>IIIIIIIIIIIIIIII', data, 0)
    (_, _, _, section_count, section_table_off,
     _, _, _, _, _, imp_off, imp_size,
     _, _, _, _) = header
    return RelHeader(section_table_off=section_table_off,
                     section_count=section_count,
                     imp_off=imp_off,
                     imp_size=imp_size)


def parse_rel_sections(data: bytes, header: RelHeader) -> List[RelSection]:
    sections: List[RelSection] = []
    for i in range(header.section_count):
        off_flags, size = struct.unpack_from('>II', data, header.section_table_off + i * 8)
        off = off_flags & 0xFFFFFFFC
        flags = off_flags & 0x3
        sections.append(RelSection(offset=off, size=size, flags=flags))
    return sections


def parse_relocations(data: bytes, header: RelHeader) -> List[Relocation]:
    relocs: List[Relocation] = []
    for i in range(0, header.imp_size, 8):
        _, relocs_off = struct.unpack_from('>II', data, header.imp_off + i)
        off = relocs_off
        curr_section = None
        curr_offset = 0
        while off + 8 <= len(data):
            delta, rel_type, rel_section = struct.unpack_from('>HBB', data, off)
            addend = struct.unpack_from('>I', data, off + 4)[0]
            off += 8
            if rel_type == R_PPC_NONE:
                break
            if rel_type == R_PPC_SECTION:
                curr_section = rel_section
                curr_offset = 0
                continue
            if curr_section is None:
                continue
            curr_offset += delta
            relocs.append(Relocation(
                patch_section=curr_section,
                patch_offset=curr_offset,
                rel_type=rel_type,
                target_section=rel_section,
                addend=addend,
            ))
    return relocs


def find_stage_world_themes_offset(data: bytes, section: RelSection) -> Optional[int]:
    start = section.offset
    end = section.offset + section.size
    max_val = 41
    for off in range(start, end - STAGE_WORLD_THEMES_LEN):
        chunk = data[off:off + STAGE_WORLD_THEMES_LEN]
        if not chunk:
            break
        if max(chunk) > max_val:
            continue
        tail = data[off + STAGE_WORLD_THEMES_LEN:off + STAGE_WORLD_THEMES_LEN + 16]
        if b'bg/' not in tail:
            continue
        return off
    return None


def parse_symbol_addresses(lst_path: Path) -> Dict[str, int]:
    symbols = {}
    pattern = re.compile(r'^(?P<addr>[0-9A-Fa-f]{8}):(?P<name>\S+)')
    for line in lst_path.read_text(encoding='ascii', errors='ignore').splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        symbols[match.group('name')] = int(match.group('addr'), 16)
    return symbols


def resolve_section_base(symbol_addr: int, symbol_file_off: int, section: RelSection) -> int:
    return symbol_addr - (symbol_file_off - section.offset)


def read_cstring(data: bytes, offset: int) -> str:
    end = data.find(b'\x00', offset)
    if end == -1:
        end = len(data)
    return data[offset:end].decode('ascii', errors='ignore')


def parse_bg_name_list(
    data: bytes,
    section: RelSection,
    relocs: List[Relocation],
    list_file_off: int,
    base_addr: int,
) -> List[Optional[str]]:
    list_offset = list_file_off - section.offset
    relocs_by_offset = {
        r.patch_offset: r
        for r in relocs
        if r.patch_section == 5 and list_offset <= r.patch_offset < list_offset + BG_NAME_COUNT * 4
    }
    names: List[Optional[str]] = []
    for idx in range(BG_NAME_COUNT):
        entry_offset = list_offset + idx * 4
        reloc = relocs_by_offset.get(entry_offset)
        if not reloc:
            ptr = read_u32_be(data, section.offset + entry_offset)
            if ptr == 0:
                names.append(None)
                continue
            if base_addr <= ptr < base_addr + section.size:
                names.append((5, ptr - base_addr))
            else:
                names.append(None)
            continue
        target_section = reloc.target_section
        if target_section <= 0:
            names.append(None)
            continue
        if target_section < 0 or target_section >= 18:
            names.append(None)
            continue
        # We only need section offsets; index lookup is fine.
        # The caller has the full section list; we patch this later.
        names.append((target_section, reloc.addend))
    return names


def resolve_bg_name_entries(
    data: bytes,
    sections: List[RelSection],
    entries: List[Optional[Tuple[int, int]]],
) -> List[Optional[str]]:
    resolved: List[Optional[str]] = []
    for entry in entries:
        if not entry:
            resolved.append(None)
            continue
        sec_idx, addend = entry
        if sec_idx < 0 or sec_idx >= len(sections):
            resolved.append(None)
            continue
        sec = sections[sec_idx]
        if sec.offset == 0:
            resolved.append(None)
            continue
        resolved.append(read_cstring(data, sec.offset + addend))
    return resolved


def parse_theme_lights(data: bytes, section: RelSection, base_addr: int, theme_addr: int) -> List[Dict[str, object]]:
    file_off = section.offset + (theme_addr - base_addr)
    lights = []
    for i in range(THEME_LIGHT_COUNT):
        off = file_off + i * 72
        floats = struct.unpack_from('>16f', data, off)
        rot_x = read_s16_be(data, off + 16 * 4)
        rot_y = read_s16_be(data, off + 16 * 4 + 2)
        lights.append({
            'ambient': [floats[1], floats[2], floats[3]],
            'infLight': [floats[13], floats[14], floats[15]],
            'rotX': rot_x,
            'rotY': rot_y,
        })
    return lights


def parse_stage_world_themes_at(data: bytes, file_off: int) -> List[int]:
    return list(data[file_off:file_off + STAGE_WORLD_THEMES_LEN])


def read_ptr_be(data: bytes, offset: int) -> Optional[int]:
    if offset is None or offset < 0 or offset + 4 > len(data):
        return None
    value = read_u32_be(data, offset)
    if value == 0 or value >= len(data):
        return None
    return value


def parse_keyframes(data: bytes, offset: Optional[int], count: int) -> Optional[List[Dict[str, float]]]:
    if offset is None or count <= 0:
        return None
    frames = []
    for i in range(count):
        base = offset + i * KEYFRAME_SIZE
        frames.append({
            'ease': float(read_s32_be(data, base)),
            't': read_f32_be(data, base + 4),
            'v': read_f32_be(data, base + 8),
            'in': read_f32_be(data, base + 0x0c),
            'out': read_f32_be(data, base + 0x10),
        })
    return frames


def parse_stage_fog(data: bytes, fog_ptr: Optional[int], fog_anim_ptr: Optional[int]) -> Optional[StageFog]:
    if fog_ptr is None:
        return None
    fog_type = read_u32_be(data, fog_ptr)
    start = read_f32_be(data, fog_ptr + 4)
    end = read_f32_be(data, fog_ptr + 8)
    color = (
        read_f32_be(data, fog_ptr + 0x0c),
        read_f32_be(data, fog_ptr + 0x10),
        read_f32_be(data, fog_ptr + 0x14),
    )
    anim = None
    if fog_anim_ptr is not None:
        start_count = read_u32_be(data, fog_anim_ptr)
        start_ptr = read_ptr_be(data, fog_anim_ptr + 4)
        end_count = read_u32_be(data, fog_anim_ptr + 8)
        end_ptr = read_ptr_be(data, fog_anim_ptr + 0x0c)
        r_count = read_u32_be(data, fog_anim_ptr + 0x10)
        r_ptr = read_ptr_be(data, fog_anim_ptr + 0x14)
        g_count = read_u32_be(data, fog_anim_ptr + 0x18)
        g_ptr = read_ptr_be(data, fog_anim_ptr + 0x1c)
        b_count = read_u32_be(data, fog_anim_ptr + 0x20)
        b_ptr = read_ptr_be(data, fog_anim_ptr + 0x24)
        anim = FogAnim(
            start=parse_keyframes(data, start_ptr, start_count),
            end=parse_keyframes(data, end_ptr, end_count),
            r=parse_keyframes(data, r_ptr, r_count),
            g=parse_keyframes(data, g_ptr, g_count),
            b=parse_keyframes(data, b_ptr, b_count),
        )
    return StageFog(fog_type=fog_type, start=start, end=end, color=color, anim=anim)


def parse_stage_env(stage_path: Path) -> Optional[StageFog]:
    raw = stage_path.read_bytes()
    decompressed = lzss_decompress(raw)
    if not decompressed:
        return None
    fog_anim_ptr = read_ptr_be(decompressed, 0xb0)
    fog_ptr = read_ptr_be(decompressed, 0xbc)
    return parse_stage_fog(decompressed, fog_ptr, fog_anim_ptr)


def read_stage_names(stgname_path: Path) -> Dict[int, str]:
    lines = stgname_path.read_text(encoding='ascii', errors='ignore').splitlines()
    names: Dict[int, str] = {}
    for idx, line in enumerate(lines):
        name = line.strip()
        if not name or name == '-':
            continue
        names[idx] = name
    return names


def list_stage_ids(stage_dir: Path) -> List[int]:
    ids = []
    for path in stage_dir.glob('STAGE*.lz'):
        match = re.match(r'^STAGE(\d{3})\.lz$', path.name)
        if not match:
            continue
        ids.append(int(match.group(1)))
    return sorted(ids)


def collect_stage_ids_from_courses(courses: Dict[str, object]) -> List[int]:
    stage_ids: List[int] = []
    challenge = courses.get('challenge') if isinstance(courses, dict) else None
    if isinstance(challenge, dict):
        order = challenge.get('order')
        if isinstance(order, dict):
            for values in order.values():
                if isinstance(values, list):
                    stage_ids.extend([int(v) for v in values if isinstance(v, int)])
    story = courses.get('story')
    if isinstance(story, list):
        for world in story:
            if isinstance(world, list):
                stage_ids.extend([int(v) for v in world if isinstance(v, int)])
    return stage_ids


def copy_file(src: Path, dst: Path, warnings: List[str]) -> None:
    if not src.exists():
        warnings.append(f'missing file: {src}')
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def find_lst_path(rom_dir: Path) -> Optional[Path]:
    for parent in [rom_dir, *rom_dir.parents]:
        candidate = parent / 'src-smb2' / 'mkb2.us.lst'
        if candidate.exists():
            return candidate
    return None


def build_pack(
    rom_dir: Path,
    out_dir: Path,
    pack_id: str,
    pack_name: str,
    courses_path: Optional[Path],
    zip_output: bool,
    courses_data: Optional[Dict[str, object]] = None,
    lst_path: Optional[Path] = None,
    stage_time_overrides: Optional[Dict[int, int]] = None,
) -> None:
    main_loop_rel = rom_dir / 'mkb2.main_loop.rel'
    stgname = rom_dir / 'stgname' / 'usa.str'
    stage_dir = rom_dir / 'stage'
    bg_dir = rom_dir / 'bg'
    init_dir = rom_dir / 'init'
    lst_path = lst_path or find_lst_path(rom_dir)

    warnings: List[str] = []

    if not main_loop_rel.exists():
        raise SystemExit(f'missing {main_loop_rel}')
    if not stgname.exists():
        raise SystemExit(f'missing {stgname}')
    if not stage_dir.exists():
        raise SystemExit(f'missing {stage_dir}')
    if not bg_dir.exists():
        warnings.append(f'missing {bg_dir}')
    if not init_dir.exists():
        warnings.append(f'missing {init_dir}')
    if lst_path and lst_path.exists():
        symbols = parse_symbol_addresses(lst_path)
    else:
        symbols = DEFAULT_SYMBOLS.copy()
        print('Warning: mkb2.us.lst not found; using default symbol addresses.')

    rel_data = main_loop_rel.read_bytes()
    rel_header = parse_rel_header(rel_data)
    sections = parse_rel_sections(rel_data, rel_header)
    relocs = parse_relocations(rel_data, rel_header)

    section5 = sections[5]
    stage_world_off = DEFAULT_STAGE_WORLD_FILE_OFF
    if not (section5.offset <= stage_world_off < section5.offset + section5.size):
        stage_world_off = find_stage_world_themes_offset(rel_data, section5)
    if stage_world_off is None:
        raise SystemExit('failed to locate STAGE_WORLD_THEMES table')

    stage_world_addr = symbols.get('STAGE_WORLD_THEMES')
    theme_lights_addr = symbols.get('theme_lights')
    if stage_world_addr is None or theme_lights_addr is None:
        raise SystemExit('missing symbols in mkb2.us.lst (STAGE_WORLD_THEMES/theme_lights)')

    base_addr = resolve_section_base(stage_world_addr, stage_world_off, section5)

    stage_world_themes = parse_stage_world_themes_at(rel_data, stage_world_off)

    bg_names = BG_NAME_TABLE

    theme_lights = parse_theme_lights(rel_data, section5, base_addr, theme_lights_addr)

    stage_ids = list_stage_ids(stage_dir)
    stage_names = read_stage_names(stgname)

    courses = None
    if courses_path:
        courses = json.loads(courses_path.read_text(encoding='utf-8'))
    elif courses_data is not None:
        courses = courses_data

    if courses:
        requested = collect_stage_ids_from_courses(courses)
        if requested:
            available = set(stage_ids)
            missing = sorted({sid for sid in requested if sid not in available})
            if missing:
                warnings.append(f'missing stages from courses: {missing}')
            stage_ids = sorted({sid for sid in requested if sid in available})
            stage_names = {k: v for k, v in stage_names.items() if k in stage_ids}

    stage_env: Dict[str, Dict[str, object]] = {}
    referenced_bgs = set()

    for stage_id in stage_ids:
        env: Dict[str, object] = {}
        if stage_id < len(stage_world_themes):
            theme_id = stage_world_themes[stage_id]
            bg_name = bg_names[theme_id] if theme_id < len(bg_names) else None
            if bg_name:
                light = theme_lights[theme_id] if theme_id < len(theme_lights) else None
                if light:
                    env['bgInfo'] = {
                        'fileName': bg_name,
                        'clearColor': [1.0, 1.0, 1.0, 1.0],
                        'ambientColor': light['ambient'],
                        'infLightColor': light['infLight'],
                        'infLightRotX': light['rotX'],
                        'infLightRotY': light['rotY'],
                    }
                else:
                    env['bgInfo'] = {
                        'fileName': bg_name,
                        'clearColor': [1.0, 1.0, 1.0, 1.0],
                    }
                referenced_bgs.add(bg_name)
        fog = parse_stage_env(stage_dir / f'STAGE{stage_id:03d}.lz')
        if fog:
            fog_obj = {
                'type': fog.fog_type,
                'start': fog.start,
                'end': fog.end,
                'color': list(fog.color),
            }
            if fog.anim:
                anim = {
                    'start': fog.anim.start,
                    'end': fog.anim.end,
                    'r': fog.anim.r,
                    'g': fog.anim.g,
                    'b': fog.anim.b,
                }
                if any(anim.values()):
                    fog_obj['anim'] = anim
            env['fog'] = fog_obj
        if env:
            stage_env[str(stage_id)] = env

    if not referenced_bgs:
        warnings.append('no backgrounds referenced from stage env data')

    content = {
        'stages': stage_ids,
        'stageNames': {str(k): v for k, v in stage_names.items()},
    }
    if stage_time_overrides:
        content['stageTimeOverrides'] = {str(k): v for k, v in stage_time_overrides.items()}

    pack_manifest = {
        'id': pack_id,
        'name': pack_name,
        'gameSource': 'smb2',
        'version': 1,
        'content': content,
        'courses': courses,
        'stageEnv': stage_env,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'init').mkdir(exist_ok=True)
    (out_dir / 'bg').mkdir(exist_ok=True)

    # Copy init
    copy_file(init_dir / 'common.lz', out_dir / 'init' / 'common.lz', warnings)
    copy_file(init_dir / 'common_p.lz', out_dir / 'init' / 'common_p.lz', warnings)
    copy_file(init_dir / 'common.gma', out_dir / 'init' / 'common.gma', warnings)
    copy_file(init_dir / 'common.tpl', out_dir / 'init' / 'common.tpl', warnings)

    # Copy stages
    for stage_id in stage_ids:
        stage_folder = out_dir / f'st{stage_id:03d}'
        stage_folder.mkdir(exist_ok=True)
        copy_file(stage_dir / f'STAGE{stage_id:03d}.lz', stage_folder / f'STAGE{stage_id:03d}.lz', warnings)
        copy_file(stage_dir / f'st{stage_id:03d}.gma', stage_folder / f'st{stage_id:03d}.gma', warnings)
        copy_file(stage_dir / f'st{stage_id:03d}.tpl', stage_folder / f'st{stage_id:03d}.tpl', warnings)

    # Copy backgrounds
    for bg_name in sorted(referenced_bgs):
        copy_file(bg_dir / f'{bg_name}.gma', out_dir / 'bg' / f'{bg_name}.gma', warnings)
        copy_file(bg_dir / f'{bg_name}.tpl', out_dir / 'bg' / f'{bg_name}.tpl', warnings)

    # Write pack.json
    (out_dir / 'pack.json').write_text(json.dumps(pack_manifest, indent=2), encoding='utf-8')

    if zip_output:
        zip_path = out_dir.with_suffix('.zip')
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(out_dir):
                for name in files:
                    file_path = Path(root) / name
                    rel_path = file_path.relative_to(out_dir)
                    zf.write(file_path, rel_path.as_posix())

    if warnings:
        print('Warnings:')
        for warning in warnings:
            print(f'  - {warning}')


def load_vanilla_courses_from_rom(
    rom_dir: Path,
) -> Tuple[Dict[str, List[Tuple[int, bool]]], List[List[int]], Dict[int, int], List[str]]:
    try:
        from dump_vanilla_conf import load_vanilla_course_data
    except Exception as exc:
        raise RuntimeError(f'failed to import dump_vanilla_conf: {exc}') from exc

    data = load_vanilla_course_data(rom_dir)
    challenge = data.get('challenge') if isinstance(data, dict) else None
    story = data.get('story') if isinstance(data, dict) else None

    course_name_map = {
        'beginner': 'Beginner',
        'beginner_extra': 'Beginner Extra',
        'advanced': 'Advanced',
        'advanced_extra': 'Advanced Extra',
        'expert': 'Expert',
        'expert_extra': 'Expert Extra',
        'master': 'Master',
        'master_extra': 'Master Extra',
    }

    challenge_courses: Dict[str, List[Tuple[int, bool]]] = {}
    story_worlds: List[List[int]] = []
    stage_time_overrides: Dict[int, int] = {}
    warnings: List[str] = []
    default_time = 60.0

    def register_time_override(stage_id: int, time_limit: Optional[float]) -> None:
        if time_limit is None:
            return
        if abs(time_limit - default_time) < 0.01:
            return
        frames = int(round(time_limit * 60))
        existing = stage_time_overrides.get(stage_id)
        if existing is not None and existing != frames:
            warnings.append(
                f'stage {stage_id} has conflicting time limits ({existing} vs {frames} frames)'
            )
            return
        stage_time_overrides[stage_id] = frames

    if isinstance(challenge, dict):
        for key, entries in challenge.items():
            name = course_name_map.get(key, key)
            course_entries: List[Tuple[int, bool]] = []
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    stage_id = entry.get('stage_id')
                    if not isinstance(stage_id, int):
                        continue
                    bonus = bool(entry.get('is_bonus_stage'))
                    course_entries.append((stage_id, bonus))
                    register_time_override(stage_id, entry.get('time_limit'))
            if course_entries:
                challenge_courses[name] = course_entries

    if isinstance(story, list):
        for world in story:
            world_entries: List[int] = []
            if isinstance(world, list):
                for entry in world:
                    if not isinstance(entry, dict):
                        continue
                    stage_id = entry.get('stage_id')
                    if not isinstance(stage_id, int):
                        continue
                    world_entries.append(stage_id)
                    register_time_override(stage_id, entry.get('time_limit'))
            if world_entries:
                story_worlds.append(world_entries)

    if not story_worlds:
        warnings.append('Story mode data not found; leaving story worlds empty.')

    return challenge_courses, story_worlds, stage_time_overrides, warnings


def parse_cmmod_config(config_path: Path) -> Tuple[
    Dict[str, List[Tuple[int, bool]]],
    Dict[int, int],
    List[str],
]:
    warnings: List[str] = []
    entry_lists: Dict[str, List[Tuple[int, Optional[int]]]] = {}
    diff_map: Dict[str, str] = {}

    def parse_num(token: str) -> Optional[int]:
        try:
            return int(token, 0)
        except ValueError:
            return None

    current_list: Optional[str] = None
    for raw_line in config_path.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = raw_line.split('%', 1)[0].strip()
        if not line:
            continue
        if line.startswith('#beginEntryList'):
            parts = line.split()
            if len(parts) >= 2:
                current_list = parts[1]
                entry_lists[current_list] = []
            else:
                warnings.append('Malformed #beginEntryList line')
            continue
        if line.startswith('#endEntryList'):
            current_list = None
            continue
        if line.startswith('#diff'):
            parts = line.split()
            if len(parts) >= 3:
                diff_name = parts[1]
                list_name = parts[2]
                diff_map[diff_name] = list_name
            else:
                warnings.append('Malformed #diff line')
            continue
        if line.startswith('#'):
            continue
        if current_list is None:
            continue
        left = line.split('|', 1)[0].strip()
        if not left:
            continue
        tokens = left.split()
        stage_id = parse_num(tokens[0])
        if stage_id is None:
            warnings.append(f'Invalid stage id in line: {raw_line}')
            continue
        time_override: Optional[int] = None
        if len(tokens) >= 2:
            time_val = parse_num(tokens[1])
            if time_val is None:
                warnings.append(f'Invalid time in line: {raw_line}')
            elif time_val != 3600:
                time_override = time_val
        entry_lists.setdefault(current_list, []).append((stage_id, time_override))

    diff_name_map = {
        'Beginner': 'Beginner',
        'Advanced': 'Advanced',
        'Expert': 'Expert',
        'BeginnerExtra': 'Beginner Extra',
        'AdvancedExtra': 'Advanced Extra',
        'ExpertExtra': 'Expert Extra',
        'Master': 'Master',
        'MasterExtra': 'Master Extra',
    }

    challenge_courses: Dict[str, List[Tuple[int, bool]]] = {}
    stage_time_overrides: Dict[int, int] = {}

    for diff_name, list_name in diff_map.items():
        display_name = diff_name_map.get(diff_name)
        if not display_name:
            continue
        entries = entry_lists.get(list_name)
        if not entries:
            warnings.append(f'Missing entry list for {diff_name}: {list_name}')
            continue
        challenge_courses[display_name] = [(stage_id, False) for stage_id, _ in entries]
        for stage_id, time_override in entries:
            if time_override is None:
                continue
            existing = stage_time_overrides.get(stage_id)
            if existing is not None and existing != time_override:
                warnings.append(
                    f'stage {stage_id} has conflicting time limits ({existing} vs {time_override})'
                )
                continue
            stage_time_overrides[stage_id] = time_override

    if not challenge_courses:
        warnings.append('No challenge courses found in cmmod config.')

    return challenge_courses, stage_time_overrides, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description='Build SMB2 pack from extracted ROM.')
    parser.add_argument('--rom', type=Path, help='Path to extracted SMB2 ROM folder')
    parser.add_argument('--out', type=Path, help='Output pack folder')
    parser.add_argument('--id', help='Pack id')
    parser.add_argument('--name', help='Pack display name')
    parser.add_argument('--courses', type=Path, help='Optional JSON file defining course lists')
    parser.add_argument('--lst', type=Path, help='Path to mkb2.us.lst (optional)')
    parser.add_argument('--zip', action='store_true', help='Also emit pack.zip')
    parser.add_argument('--gui', action='store_true', help='Launch a simple GUI')
    args = parser.parse_args()

    if args.gui:
        run_gui()
        return
    if not args.rom or not args.out or not args.id or not args.name:
        parser.error('--rom, --out, --id, and --name are required unless --gui is used')
    build_pack(args.rom, args.out, args.id, args.name, args.courses, args.zip, lst_path=args.lst)


def run_gui() -> None:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        print('Tkinter is required for the GUI. Install it or run without --gui.', file=sys.stderr)
        return

    root = tk.Tk()
    root.title('SMB2 Pack Builder')
    root.geometry('920x640')

    frame = ttk.Frame(root, padding=10)
    frame.pack(fill=tk.BOTH, expand=True)

    config_frame = ttk.LabelFrame(frame, text='Pack Config', padding=10)
    config_frame.pack(fill=tk.X, pady=(0, 10))

    rom_var = tk.StringVar()
    out_var = tk.StringVar()
    pack_id_var = tk.StringVar()
    pack_name_var = tk.StringVar()
    lst_var = tk.StringVar()
    zip_var = tk.BooleanVar(value=True)

    last_rom_dir = ''
    last_out_dir = ''

    def browse_dir(target_var: tk.StringVar, last_dir_attr: str):
        nonlocal last_rom_dir, last_out_dir
        initial = last_rom_dir if last_dir_attr == 'rom' else last_out_dir
        value = filedialog.askdirectory(initialdir=initial or None)
        if value:
            target_var.set(value)
            if last_dir_attr == 'rom':
                last_rom_dir = value
            else:
                last_out_dir = value

    ttk.Label(config_frame, text='ROM folder').grid(row=0, column=0, sticky=tk.W, padx=4, pady=4)
    ttk.Entry(config_frame, textvariable=rom_var, width=60).grid(row=0, column=1, sticky=tk.W, padx=4, pady=4)
    ttk.Button(config_frame, text='Browse', command=lambda: browse_dir(rom_var, 'rom')).grid(row=0, column=2, padx=4, pady=4)

    ttk.Label(config_frame, text='Output folder').grid(row=1, column=0, sticky=tk.W, padx=4, pady=4)
    ttk.Entry(config_frame, textvariable=out_var, width=60).grid(row=1, column=1, sticky=tk.W, padx=4, pady=4)
    ttk.Button(config_frame, text='Browse', command=lambda: browse_dir(out_var, 'out')).grid(row=1, column=2, padx=4, pady=4)

    ttk.Label(config_frame, text='Pack ID').grid(row=2, column=0, sticky=tk.W, padx=4, pady=4)
    ttk.Entry(config_frame, textvariable=pack_id_var, width=30).grid(row=2, column=1, sticky=tk.W, padx=4, pady=4)

    ttk.Label(config_frame, text='Pack name').grid(row=3, column=0, sticky=tk.W, padx=4, pady=4)
    ttk.Entry(config_frame, textvariable=pack_name_var, width=30).grid(row=3, column=1, sticky=tk.W, padx=4, pady=4)

    ttk.Label(config_frame, text='mkb2.us.lst').grid(row=4, column=0, sticky=tk.W, padx=4, pady=4)
    ttk.Entry(config_frame, textvariable=lst_var, width=60).grid(row=4, column=1, sticky=tk.W, padx=4, pady=4)
    ttk.Button(
        config_frame,
        text='Browse',
        command=lambda: lst_var.set(filedialog.askopenfilename(filetypes=[('mkb2.us.lst', '*.lst'), ('All files', '*.*')])),
    ).grid(row=4, column=2, padx=4, pady=4)

    ttk.Checkbutton(config_frame, text='Also emit pack.zip', variable=zip_var).grid(row=5, column=1, sticky=tk.W, padx=4, pady=4)

    courses_frame = ttk.Frame(frame)
    courses_frame.pack(fill=tk.BOTH, expand=True)

    load_controls = ttk.Frame(courses_frame)
    load_controls.pack(fill=tk.X, pady=(0, 8))

    challenge_frame = ttk.LabelFrame(courses_frame, text='Challenge Courses', padding=10)
    story_frame = ttk.LabelFrame(courses_frame, text='Story Worlds', padding=10)
    challenge_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
    story_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    challenge_courses: Dict[str, List[Tuple[int, bool]]] = {}
    stage_time_overrides: Dict[int, int] = {}
    selected_course_name = tk.StringVar()

    course_list = tk.Listbox(challenge_frame, height=8)
    course_list.pack(fill=tk.X, pady=(0, 6))

    def refresh_course_list():
        course_list.delete(0, tk.END)
        for name in sorted(challenge_courses.keys()):
            course_list.insert(tk.END, name)

    def on_course_select(_event=None):
        selection = course_list.curselection()
        if not selection:
            selected_course_name.set('')
            refresh_stage_list()
            return
        name = course_list.get(selection[0])
        selected_course_name.set(name)
        refresh_stage_list()

    course_list.bind('<<ListboxSelect>>', on_course_select)

    course_controls = ttk.Frame(challenge_frame)
    course_controls.pack(fill=tk.X, pady=(0, 10))

    course_name_var = tk.StringVar()
    ttk.Entry(course_controls, textvariable=course_name_var, width=20).pack(side=tk.LEFT, padx=(0, 6))

    def add_course():
        name = course_name_var.get().strip()
        if not name:
            messagebox.showerror('Missing name', 'Enter a course name.')
            return
        if name in challenge_courses:
            messagebox.showerror('Duplicate name', 'Course already exists.')
            return
        challenge_courses[name] = []
        course_name_var.set('')
        refresh_course_list()

    def remove_course():
        selection = course_list.curselection()
        if not selection:
            return
        name = course_list.get(selection[0])
        challenge_courses.pop(name, None)
        selected_course_name.set('')
        refresh_course_list()
        refresh_stage_list()

    ttk.Button(course_controls, text='Add course', command=add_course).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(course_controls, text='Remove', command=remove_course).pack(side=tk.LEFT)

    stage_list = tk.Listbox(challenge_frame, height=10)
    stage_list.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

    def refresh_stage_list():
        stage_list.delete(0, tk.END)
        name = selected_course_name.get()
        if not name:
            return
        for stage_id, bonus in challenge_courses.get(name, []):
            time_override = stage_time_overrides.get(stage_id)
            time_label = f' ({time_override // 60}s)' if time_override else ''
            label = f'{stage_id}{time_label} {"(bonus)" if bonus else ""}'
            stage_list.insert(tk.END, label.strip())

    stage_controls = ttk.Frame(challenge_frame)
    stage_controls.pack(fill=tk.X)

    stage_id_var = tk.StringVar()
    stage_bonus_var = tk.BooleanVar(value=False)
    stage_time_var = tk.StringVar()
    stage_id_entry = ttk.Entry(stage_controls, textvariable=stage_id_var, width=8)
    stage_id_entry.pack(side=tk.LEFT, padx=(0, 6))
    ttk.Checkbutton(stage_controls, text='Bonus', variable=stage_bonus_var).pack(side=tk.LEFT, padx=(0, 6))
    stage_time_entry = ttk.Entry(stage_controls, textvariable=stage_time_var, width=6)
    stage_time_entry.pack(side=tk.LEFT, padx=(0, 6))
    ttk.Label(stage_controls, text='sec').pack(side=tk.LEFT, padx=(0, 6))

    def add_stage():
        name = selected_course_name.get()
        if not name:
            messagebox.showerror('No course', 'Select a course first.')
            return
        raw = stage_id_var.get().strip()
        if not raw.isdigit():
            messagebox.showerror('Invalid stage', 'Stage ID must be a number.')
            return
        raw_time = stage_time_var.get().strip()
        if raw_time:
            if not raw_time.isdigit():
                messagebox.showerror('Invalid time', 'Time must be a number of seconds.')
                return
            stage_time_overrides[int(raw)] = int(raw_time) * 60
        stage_id = int(raw)
        bonus = bool(stage_bonus_var.get())
        challenge_courses[name].append((stage_id, bonus))
        stage_id_var.set('')
        stage_time_var.set('')
        stage_bonus_var.set(False)
        refresh_stage_list()
        stage_id_entry.focus_set()

    def stage_in_use(stage_id: int) -> bool:
        for entries in challenge_courses.values():
            if any(stage_id == sid for sid, _ in entries):
                return True
        for world in story_worlds:
            if stage_id in world:
                return True
        return False

    def remove_stage():
        name = selected_course_name.get()
        selection = stage_list.curselection()
        if not name or not selection:
            return
        idx = selection[0]
        items = challenge_courses.get(name, [])
        if 0 <= idx < len(items):
            stage_id, _ = items.pop(idx)
            if not stage_in_use(stage_id):
                stage_time_overrides.pop(stage_id, None)
        refresh_stage_list()

    def toggle_bonus():
        name = selected_course_name.get()
        selection = stage_list.curselection()
        if not name or not selection:
            return
        idx = selection[0]
        items = challenge_courses.get(name, [])
        if 0 <= idx < len(items):
            stage_id, bonus = items[idx]
            items[idx] = (stage_id, not bonus)
        refresh_stage_list()

    ttk.Button(stage_controls, text='Add stage', command=add_stage).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(stage_controls, text='Remove', command=remove_stage).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(stage_controls, text='Toggle bonus', command=toggle_bonus).pack(side=tk.LEFT)
    stage_id_entry.bind('<Return>', lambda _event: add_stage())
    stage_time_entry.bind('<Return>', lambda _event: add_stage())

    story_worlds: List[List[int]] = []
    world_list = tk.Listbox(story_frame, height=8)
    world_list.pack(fill=tk.X, pady=(0, 6))

    def refresh_world_list():
        world_list.delete(0, tk.END)
        for idx in range(len(story_worlds)):
            world_list.insert(tk.END, f'World {idx + 1}')

    def on_world_select(_event=None):
        refresh_world_stage_list()

    world_list.bind('<<ListboxSelect>>', on_world_select)

    world_controls = ttk.Frame(story_frame)
    world_controls.pack(fill=tk.X, pady=(0, 10))

    def add_world():
        story_worlds.append([])
        refresh_world_list()

    def remove_world():
        selection = world_list.curselection()
        if not selection:
            return
        idx = selection[0]
        if 0 <= idx < len(story_worlds):
            story_worlds.pop(idx)
        refresh_world_list()
        refresh_world_stage_list()

    ttk.Button(world_controls, text='Add world', command=add_world).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(world_controls, text='Remove', command=remove_world).pack(side=tk.LEFT)

    world_stage_list = tk.Listbox(story_frame, height=10)
    world_stage_list.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

    def refresh_world_stage_list():
        world_stage_list.delete(0, tk.END)
        selection = world_list.curselection()
        if not selection:
            return
        idx = selection[0]
        for stage_id in story_worlds[idx]:
            world_stage_list.insert(tk.END, str(stage_id))

    world_stage_controls = ttk.Frame(story_frame)
    world_stage_controls.pack(fill=tk.X)

    world_stage_id_var = tk.StringVar()
    world_stage_time_var = tk.StringVar()
    world_stage_entry = ttk.Entry(world_stage_controls, textvariable=world_stage_id_var, width=8)
    world_stage_entry.pack(side=tk.LEFT, padx=(0, 6))
    world_stage_time_entry = ttk.Entry(world_stage_controls, textvariable=world_stage_time_var, width=6)
    world_stage_time_entry.pack(side=tk.LEFT, padx=(0, 6))
    ttk.Label(world_stage_controls, text='sec').pack(side=tk.LEFT, padx=(0, 6))

    def add_world_stage():
        selection = world_list.curselection()
        if not selection:
            messagebox.showerror('No world', 'Select a world first.')
            return
        raw = world_stage_id_var.get().strip()
        if not raw.isdigit():
            messagebox.showerror('Invalid stage', 'Stage ID must be a number.')
            return
        raw_time = world_stage_time_var.get().strip()
        if raw_time:
            if not raw_time.isdigit():
                messagebox.showerror('Invalid time', 'Time must be a number of seconds.')
                return
            stage_time_overrides[int(raw)] = int(raw_time) * 60
        idx = selection[0]
        story_worlds[idx].append(int(raw))
        world_stage_id_var.set('')
        world_stage_time_var.set('')
        refresh_world_stage_list()
        world_stage_entry.focus_set()

    def remove_world_stage():
        selection = world_list.curselection()
        stage_selection = world_stage_list.curselection()
        if not selection or not stage_selection:
            return
        world_idx = selection[0]
        stage_idx = stage_selection[0]
        if 0 <= stage_idx < len(story_worlds[world_idx]):
            stage_id = story_worlds[world_idx].pop(stage_idx)
            if not stage_in_use(stage_id):
                stage_time_overrides.pop(stage_id, None)
        refresh_world_stage_list()

    ttk.Button(world_stage_controls, text='Add stage', command=add_world_stage).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(world_stage_controls, text='Remove', command=remove_world_stage).pack(side=tk.LEFT)
    world_stage_entry.bind('<Return>', lambda _event: add_world_stage())
    world_stage_time_entry.bind('<Return>', lambda _event: add_world_stage())

    def build_courses_data() -> Dict[str, object]:
        order = {}
        bonus = {}
        for name, entries in challenge_courses.items():
            order[name] = [stage_id for stage_id, _ in entries]
            bonus[name] = [flag for _, flag in entries]
        courses: Dict[str, object] = {
            'challenge': {
                'order': order,
                'bonus': bonus,
            },
        }
        if story_worlds:
            courses['story'] = story_worlds
        return courses

    def load_from_rom_clicked():
        rom_path = Path(rom_var.get().strip())
        if not rom_path.exists():
            messagebox.showerror('Invalid ROM', 'ROM folder does not exist.')
            return
        if challenge_courses or story_worlds:
            if not messagebox.askyesno(
                'Replace courses',
                'Replace current course data with values from the ROM?',
            ):
                return
        try:
            loaded_courses, loaded_worlds, loaded_overrides, load_warnings = (
                load_vanilla_courses_from_rom(rom_path)
            )
        except Exception as exc:
            messagebox.showerror('Load failed', str(exc))
            return
        challenge_courses.clear()
        challenge_courses.update(loaded_courses)
        story_worlds.clear()
        story_worlds.extend(loaded_worlds)
        stage_time_overrides.clear()
        stage_time_overrides.update(loaded_overrides)
        selected_course_name.set('')
        refresh_course_list()
        refresh_stage_list()
        refresh_world_list()
        refresh_world_stage_list()
        if load_warnings:
            messagebox.showwarning('Loaded with warnings', '\n'.join(load_warnings))

    def load_from_cmmod_clicked():
        config_path_str = filedialog.askopenfilename(
            title='Select cmmod config',
            filetypes=[('Config files', '*.txt *.cfg'), ('All files', '*.*')],
        )
        if not config_path_str:
            return
        config_path = Path(config_path_str)
        if challenge_courses or story_worlds:
            if not messagebox.askyesno(
                'Replace courses',
                'Replace current challenge courses with values from the cmmod config?',
            ):
                return
        try:
            loaded_courses, loaded_overrides, load_warnings = parse_cmmod_config(config_path)
        except Exception as exc:
            messagebox.showerror('Load failed', str(exc))
            return
        challenge_courses.clear()
        challenge_courses.update(loaded_courses)
        stage_time_overrides.clear()
        stage_time_overrides.update(loaded_overrides)
        selected_course_name.set('')
        refresh_course_list()
        refresh_stage_list()
        if story_worlds:
            load_warnings.append('Story worlds were left unchanged.')
        if load_warnings:
            messagebox.showwarning('Loaded with warnings', '\n'.join(load_warnings))

    def build_pack_clicked():
        rom_path = Path(rom_var.get().strip())
        out_path = Path(out_var.get().strip())
        pack_id = pack_id_var.get().strip()
        pack_name = pack_name_var.get().strip()
        if not rom_path.exists():
            messagebox.showerror('Invalid ROM', 'ROM folder does not exist.')
            return
        if not out_path:
            messagebox.showerror('Invalid output', 'Output folder is required.')
            return
        if not pack_id:
            messagebox.showerror('Missing ID', 'Pack ID is required.')
            return
        if not pack_name:
            messagebox.showerror('Missing name', 'Pack name is required.')
            return
        courses_data = build_courses_data()
        try:
            build_pack(
                rom_path,
                out_path,
                pack_id,
                pack_name,
                None,
                bool(zip_var.get()),
                courses_data=courses_data,
                lst_path=Path(lst_var.get().strip()) if lst_var.get().strip() else None,
                stage_time_overrides=stage_time_overrides,
            )
        except Exception as exc:
            messagebox.showerror('Build failed', str(exc))
            return
        messagebox.showinfo('Done', 'Pack build completed.')

    action_frame = ttk.Frame(frame)
    action_frame.pack(fill=tk.X, pady=(10, 0))
    ttk.Button(action_frame, text='Build Pack', command=build_pack_clicked).pack(side=tk.RIGHT)
    ttk.Button(load_controls, text='Load courses from cmmod config', command=load_from_cmmod_clicked).pack(side=tk.LEFT)

    root.mainloop()


if __name__ == '__main__':
    main()
