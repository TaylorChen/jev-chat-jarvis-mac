#!/usr/bin/env python3
"""debug_xor.py — V2 缩略图格式爆破：单字节 XOR + 偏移扫描"""
import sys
from pathlib import Path

sys.path.insert(0, 'src')
from live_db import LiveProvider

p = LiveProvider()
msgs = p.messages('12345678900@chatroom', limit=100, include_non_text=True)
paths = [m['img_path'] for m in msgs if m.get('img_path')][:3]
files = [Path(f).read_bytes() for f in paths]

MAGICS = {'JPEG': b'\xff\xd8\xff', 'PNG': b'\x89\x50\x4e\x47'}

print('文件头（前 24 字节）:')
for f in files:
    print(' ', f[:24].hex())

# 假设：V2 头之后某偏移开始是 XOR 加密的图像数据
# 单字节 XOR：K = data[i] ^ magic[0]，全文件验证
for off in range(0, 20):
    for key in range(256):
        d0 = files[0][off] ^ key
        d1 = files[1][off] ^ key
        d2 = files[2][off] ^ key
        if d0 == 0xFF and d1 == 0xFF and d2 == 0xFF:  # 三个文件同偏移都是 FF
            # 验证续：下两个字节应为 D8 FF（JPEG）
            ok = all((files[i][off+1] ^ key) == 0xD8 and (files[i][off+2] ^ key) == 0xFF
                     for i in range(3))
            if ok:
                print(f'✓ 命中: 偏移 {off}，XOR key = 0x{key:02x}（JPEG）')
                # 解出第一个文件看尺寸
                dec = bytes(b ^ key for b in files[0][off:])
                print('  解密头:', dec[:12].hex())
                sys.exit(0)
print('✗ 未找到单字节 XOR 组合（V2 是更强的加密/压缩，需要别的方法）')
