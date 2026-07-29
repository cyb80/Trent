# -*- coding: utf-8 -*-
"""本地数据管道模拟运行测试

依次执行:
  1. 检查 xlsx 文件
  2. 运行 fetch_rsrs.py  → 验证 rsrs_data.js
  3. 运行 fetch_data.py  → 验证 stock_bond_data.js + graham_data.js
"""

import json
import os
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_DIR, 'data')
XLSX_PATH = os.path.join(DATA_DIR, '指数行情序列.xlsx')
SCRIPTS = ['fetch_rsrs.py', 'fetch_data.py']
OUT_FILES = ['rsrs_data.js', 'stock_bond_data.js', 'graham_data.js']

pass_count = 0
fail_count = 0


def check(desc, cond):
    global pass_count, fail_count
    if cond:
        print(f'  [PASS] {desc}')
        pass_count += 1
    else:
        print(f'  [FAIL] {desc}')
        fail_count += 1


def verify_js(path, key_name, required_indices=None):
    """验证JS文件是否可解析，包含所需key，数据非空"""
    if not os.path.exists(path):
        check(f'{os.path.basename(path)} 文件存在', False)
        return None

    size = os.path.getsize(path)
    check(f'{os.path.basename(path)} 文件存在 ({size:,} bytes)', size > 100)

    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read()
        data = json.loads(raw.replace(f'window.{key_name} = ', '').replace(';\n', ''))
        check(f'{os.path.basename(path)} JSON解析成功', True)

        if required_indices:
            for idx in required_indices:
                check(f'  包含 "{idx}"', idx in data)
                if idx in data:
                    d = data[idx]
                    perf = d.get('performance') or d.get('strategies', {}).get('RSRS_原始', {}).get('performance')
                    check(f'  {idx} 数据非空', len(d.get('dates', [])) > 10)
                    if perf:
                        check(f'  {idx} 年化={perf.get("annual_return","?")}', True)
        return data
    except Exception as e:
        check(f'{os.path.basename(path)} 解析: {e}', False)
        return None


def main():
    global pass_count, fail_count
    print('=' * 60)
    print('量化择时数据管道 — 本地模拟测试')
    print(f'项目目录: {PROJECT_DIR}')
    print(f'Python:    {sys.version.split()[0]}')
    print('=' * 60)

    # ===== Step 0: 环境检查 =====
    print('\n[0/3] 环境检查')
    check('xlsx 文件存在', os.path.exists(XLSX_PATH))
    if os.path.exists(XLSX_PATH):
        size = os.path.getsize(XLSX_PATH)
        check(f'xlsx 文件大小 ({size:,} bytes)', size > 10_000)

    for fn in SCRIPTS:
        check(f'{fn} 存在', os.path.exists(os.path.join(PROJECT_DIR, fn)))

    if fail_count > 0:
        print('\n环境检查未通过，中止测试。')
        sys.exit(1)

    # ===== Step 1: 运行 fetch_rsrs.py =====
    print('\n[1/3] 运行 fetch_rsrs.py ...')
    rsrs_path = os.path.join(PROJECT_DIR, 'fetch_rsrs.py')
    result = subprocess.run(
        [sys.executable, rsrs_path],
        capture_output=True, text=True, cwd=PROJECT_DIR, timeout=600
    )
    print('  stdout:')
    for line in result.stdout.splitlines():
        print(f'    {line}')

    if result.returncode != 0:
        print('  stderr:')
        for line in result.stderr.splitlines():
            print(f'    {line}')

    check('fetch_rsrs.py 退出码=0', result.returncode == 0)

    # 验证rsrs_data.js
    js_path = os.path.join(DATA_DIR, 'rsrs_data.js')
    data = verify_js(js_path, 'RSRS_DATA',
                     required_indices=['上证50', '沪深300', '中证500', '中证1000'])

    if data:
        for idx in ['上证50', '沪深300', '中证500', '中证1000']:
            d = data[idx]
            strats = d.get('strategies', {})
            check(f'{idx} 含4种策略', len(strats) == 4)
            check(f'{idx} 日期={len(d["dates"])}条, {d["dates"][0]}~{d["dates"][-1]}', len(d['dates']) > 100)

    # ===== Step 2: 运行 fetch_data.py =====
    print('\n[2/3] 运行 fetch_data.py ...')
    data_path = os.path.join(PROJECT_DIR, 'fetch_data.py')
    result = subprocess.run(
        [sys.executable, data_path],
        capture_output=True, text=True, cwd=PROJECT_DIR, timeout=300
    )
    print('  stdout:')
    for line in result.stdout.splitlines():
        print(f'    {line}')

    if result.returncode != 0:
        print('  stderr:')
        for line in result.stderr.splitlines():
            print(f'    {line}')

    check('fetch_data.py 退出码=0', result.returncode == 0)

    # 验证stock_bond_data.js
    sb_path = os.path.join(DATA_DIR, 'stock_bond_data.js')
    sb_data = verify_js(sb_path, 'STOCK_BOND_DATA',
                        required_indices=['上证50', '沪深300', '中证500', '中证1000'])

    # 验证graham_data.js
    gr_path = os.path.join(DATA_DIR, 'graham_data.js')
    gr_data = verify_js(gr_path, 'GRAHAM_DATA',
                        required_indices=['上证50', '沪深300', '中证500', '中证1000'])

    # 验证一致性
    if sb_data and gr_data:
        for idx in ['上证50', '沪深300', '中证500', '中证1000']:
            if idx in sb_data and idx in gr_data:
                sb_d = sb_data[idx]
                gr_d = gr_data[idx]
                check(f'{idx} 股债性价比≠格雷厄姆指数',
                      sb_d.get('stock_bond_spread') != gr_d.get('graham_index'))
                check(f'{idx} dates一致',
                      sb_d.get('dates') == gr_d.get('dates'))

    # ===== Step 3: 汇总 =====
    print('\n[3/3] 文件清单')
    for fn in OUT_FILES:
        fp = os.path.join(DATA_DIR, fn)
        if os.path.exists(fp):
            size = os.path.getsize(fp)
            print(f'  {fn:30s}  {size:>8,} bytes')
        else:
            print(f'  {fn:30s}  不存在!')

    print()
    print('=' * 60)
    total = pass_count + fail_count
    print(f'结果: {pass_count}/{total} 通过, {fail_count} 失败')
    print('=' * 60)

    return 0 if fail_count == 0 else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n测试被用户中断')
        sys.exit(1)
    except Exception as e:
        print(f'\n测试异常: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
