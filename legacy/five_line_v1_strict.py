import baostock as bs
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import warnings
import os
from typing import List, Dict, Optional
import traceback
from tqdm import tqdm
import logging
import socket

from func_timeout import func_timeout, FunctionTimedOut

warnings.filterwarnings('ignore')

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('stock_selector.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class DailySelector:
    """
    五线多头排列选股策略（增强稳定性版）
    选股条件：
        1. 五线多头排列：MA5 > MA10 > MA20 > MA30 > MA60
        2. 收盘价 >= MA10
        3. 五条均线斜率均 > 0 且 MA5斜率 > MA10斜率 > MA20斜率 > MA30斜率 > MA60斜率
        4. 10日涨幅 < 15%
        5. 成交量放大：当日量 > 5日均量 且 > 10日均量
    同时满足以上五个条件即输出。
    """

    def __init__(self,
                 ma_windows=[5, 10, 20, 30, 60],
                 max_10d_rise=0.15,
                 checkpoint_file='checkpoint.csv',
                 stock_timeout=8,          # 修改为 8 秒
                 use_cache=True):
        self.max_retries = 3
        self.request_delay = 0.3
        self.is_logged_in = False

        self.ma_windows = ma_windows
        self.max_10d_rise = max_10d_rise
        self.checkpoint_file = checkpoint_file
        self.stock_timeout = stock_timeout
        self.use_cache = use_cache

    # -------------------- 登录/登出（含预热） --------------------
    def login(self) -> bool:
        for attempt in range(self.max_retries):
            try:
                time.sleep(self.request_delay)
                lg = bs.login()
                if lg.error_code == '0':
                    self.is_logged_in = True
                    logger.info("✅ 登录成功")
                    try:
                        today = datetime.now().strftime('%Y-%m-%d')
                        rs = bs.query_trade_dates(start_date=today, end_date=today)
                        if rs.error_code == '0':
                            logger.info("🔥 连接预热成功")
                        else:
                            logger.warning(f"连接预热返回错误: {rs.error_msg}")
                    except Exception as warm_e:
                        logger.warning(f"连接预热失败（不影响登录）: {warm_e}")
                    return True
                logger.warning(f"登录失败: {lg.error_msg}，重试 {attempt+1}/{self.max_retries}")
            except Exception as e:
                logger.error(f"登录异常: {e}")
            time.sleep(2 ** attempt)
        return False

    def logout(self):
        if self.is_logged_in:
            try:
                bs.logout()
                self.is_logged_in = False
                logger.info("✅ 已登出")
            except Exception:
                pass

    # -------------------- 数据获取 --------------------
    def get_daily_data(self, code: str, end_date: str, days: int = 150) -> Optional[pd.DataFrame]:
        start_date = (datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=days*2)).strftime('%Y-%m-%d')
        fields = "date,code,open,high,low,close,volume,amount,turn"

        max_reconnects = 2
        reconnect_count = 0

        for attempt in range(self.max_retries):
            try:
                delay = self.request_delay + (2 ** attempt) + np.random.uniform(0, 1)
                time.sleep(delay)

                rs = bs.query_history_k_data_plus(
                    code, fields,
                    start_date=start_date, end_date=end_date,
                    frequency="d", adjustflag="3"
                )

                if rs.error_code != '0':
                    error_msg = rs.error_msg.lower() if rs.error_msg else ''
                    if any(kw in error_msg for kw in ['login', 'session', 'expired', '未登录', '令牌']):
                        if reconnect_count < max_reconnects:
                            logger.warning(f"[{code}] 登录过期，尝试重新登录（{reconnect_count+1}/{max_reconnects}）")
                            self.logout()
                            if self.login():
                                reconnect_count += 1
                                continue
                            else:
                                logger.error("重新登录失败，放弃重试")
                                return None
                    if rs.error_code.startswith('10001'):
                        return None
                    logger.warning(f"[{code}] 数据获取错误: {rs.error_msg}，尝试 {attempt+1}/{self.max_retries}")
                    continue

                data_list = []
                while (rs.error_code == '0') & rs.next():
                    data_list.append(rs.get_row_data())
                if not data_list:
                    return None

                df = pd.DataFrame(data_list, columns=fields.split(','))
                numeric_cols = ['open','high','low','close','volume','amount','turn']
                for col in numeric_cols:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                df['date'] = pd.to_datetime(df['date'])
                df = df.dropna(subset=['date','close'])
                df = df.sort_values('date').reset_index(drop=True)
                if len(df) > days:
                    df = df.tail(days)
                return df

            except (socket.timeout, ConnectionResetError, OSError) as e:
                logger.warning(f"[{code}] 网络异常 ({type(e).__name__})，重试 {attempt+1}/{self.max_retries}: {e}")
                if reconnect_count < max_reconnects and attempt == self.max_retries - 1:
                    logger.info("网络持续异常，尝试重新登录...")
                    self.logout()
                    if self.login():
                        reconnect_count += 1
                        continue
                if attempt == self.max_retries - 1:
                    return None
            except Exception as e:
                logger.error(f"[{code}] 其他异常，不再重试: {e}")
                return None
        return None

    def get_stock_name(self, code: str) -> str:
        for attempt in range(self.max_retries):
            try:
                rs = bs.query_stock_basic(code=code)
                if rs.error_code == '0' and rs.next():
                    return rs.get_row_data()[1]
            except Exception:
                if attempt < self.max_retries - 1:
                    time.sleep(0.5)
                    continue
        return "未知"

    # -------------------- 技术指标计算 --------------------
    @staticmethod
    def MA(series, window):
        return series.rolling(window=window, min_periods=window).mean()

    # -------------------- 选股条件判断 --------------------
    def check_stock(self, df: pd.DataFrame) -> Dict:
        max_window = max(self.ma_windows)
        if df is None or len(df) < max_window + 10:
            return {'selected': False, 'info': {}}

        for w in self.ma_windows:
            df[f'ma{w}'] = self.MA(df['close'], w)

        df['vol_ma5'] = self.MA(df['volume'], 5)
        df['vol_ma10'] = self.MA(df['volume'], 10)

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else None

        ma_cols = [f'ma{w}' for w in self.ma_windows]
        if any(pd.isna(latest[col]) for col in ma_cols):
            return {'selected': False, 'info': {}}
        if prev is None or any(pd.isna(prev[col]) for col in ma_cols):
            return {'selected': False, 'info': {}}

        # 条件1：五线多头排列
        ma_values = [latest[f'ma{w}'] for w in self.ma_windows]
        cond1 = all(ma_values[i] > ma_values[i+1] for i in range(len(ma_values)-1))

        # 条件2：收盘价 >= MA10
        cond2 = latest['close'] >= latest['ma10']

        # 条件3：均线斜率 > 0 且斜率递减
        slopes = []
        for w in self.ma_windows:
            slope = latest[f'ma{w}'] - prev[f'ma{w}']
            slopes.append(slope)
        cond3_positive = all(s > 0 for s in slopes)
        cond3_order = all(slopes[i] > slopes[i+1] for i in range(len(slopes)-1))
        cond3 = cond3_positive and cond3_order

        # 条件4：10日涨幅 < 15%
        if len(df) < 11:
            return {'selected': False, 'info': {}}
        close_10d_ago = df.iloc[-11]['close']
        if pd.isna(close_10d_ago) or close_10d_ago == 0:
            return {'selected': False, 'info': {}}
        rise_10d = (latest['close'] - close_10d_ago) / close_10d_ago
        cond4 = rise_10d < self.max_10d_rise

        # 条件5：成交量放大
        if pd.isna(latest['vol_ma5']) or pd.isna(latest['vol_ma10']):
            return {'selected': False, 'info': {}}
        cond5 = latest['volume'] > latest['vol_ma5'] and latest['volume'] > latest['vol_ma10']

        selected = cond1 and cond2 and cond3 and cond4 and cond5

        info = {
            'date': latest['date'].strftime('%Y-%m-%d'),
            'close': round(latest['close'], 2),
            'ma5': round(latest['ma5'], 2),
            'ma10': round(latest['ma10'], 2),
            'ma20': round(latest['ma20'], 2),
            'ma30': round(latest['ma30'], 2),
            'ma60': round(latest['ma60'], 2),
            'rise_10d': round(rise_10d * 100, 2),
            'volume': int(latest['volume']),
            'vol_ma5': int(latest['vol_ma5']),
            'vol_ma10': int(latest['vol_ma10'])
        }

        return {'selected': selected, 'info': info}

    # -------------------- 单只股票处理 --------------------
    def _process_one_stock(self, code: str, end_date: str) -> Optional[Dict]:
        df = self.get_daily_data(code, end_date, days=150)
        if df is None:
            return None

        res = self.check_stock(df)
        if res['selected']:
            info = res['info']
            output = {
                '股票代码': code,
                '股票名称': self.get_stock_name(code),
                '分析日期': end_date,
                '最新日期': info['date'],
                '最新收盘': info['close'],
                'MA5': info['ma5'],
                'MA10': info['ma10'],
                'MA20': info['ma20'],
                'MA30': info['ma30'],
                'MA60': info['ma60'],
                '10日涨幅(%)': info['rise_10d'],
                '当日成交量': info['volume'],
                '5日均量': info['vol_ma5'],
                '10日均量': info['vol_ma10']
            }
            return output
        else:
            return None

    # -------------------- 缓存管理 --------------------
    def _load_processed_codes(self) -> set:
        processed_file = 'processed_codes.csv'
        if not os.path.exists(processed_file):
            return set()
        try:
            df = pd.read_csv(processed_file, dtype=str)
            if 'code' in df.columns:
                return set(df['code'].tolist())
        except Exception as e:
            logger.warning(f"读取已处理列表失败: {e}")
            return set()

    def _save_processed_code(self, code: str):
        processed_file = 'processed_codes.csv'
        try:
            with open(processed_file, 'a', encoding='utf-8') as f:
                f.write(f"{code}\n")
        except Exception as e:
            logger.error(f"保存已处理代码 {code} 失败: {e}")

    # -------------------- 批量分析 --------------------
    def batch_analyze(self, stock_codes: List[str], end_date: str) -> List[Dict]:
        results = []

        if self.use_cache:
            processed_codes = self._load_processed_codes()
            if os.path.exists(self.checkpoint_file):
                try:
                    df_check = pd.read_csv(self.checkpoint_file, encoding='utf-8-sig')
                    if not df_check.empty and '股票代码' in df_check.columns:
                        processed_codes.update(df_check['股票代码'].tolist())
                        results = df_check.to_dict('records')
                        logger.info(f"📂 从旧检查点恢复结果: {len(results)} 只")
                except Exception as e:
                    logger.warning(f"检查点文件读取异常: {e}")
            if processed_codes:
                logger.info(f"📂 已处理代码数: {len(processed_codes)}")
        else:
            processed_codes = set()
            for f in ['processed_codes.csv', self.checkpoint_file]:
                if os.path.exists(f):
                    os.remove(f)
            logger.info("🔄 不使用缓存，将重新开始分析")

        remaining_codes = [c for c in stock_codes if c not in processed_codes]
        logger.info(f"开始批量分析：共 {len(stock_codes)} 只，已处理 {len(processed_codes)} 只，剩余 {len(remaining_codes)} 只")

        with tqdm(total=len(remaining_codes), desc="分析进度", initial=0) as pbar:
            for idx, code in enumerate(remaining_codes, 1):
                try:
                    output = func_timeout(
                        self.stock_timeout,
                        self._process_one_stock,
                        args=(code, end_date)
                    )
                    if output is not None:
                        results.append(output)
                        logger.debug(f"✅ {code} 满足五线多头条件")
                except FunctionTimedOut:
                    logger.warning(f"[{code}] 整体处理超时（{self.stock_timeout}秒），自动跳过")
                except Exception as e:
                    logger.error(f"[{code}] 处理失败: {e}")
                finally:
                    self._save_processed_code(code)
                    processed_codes.add(code)

                pbar.update(1)

                if idx % 20 == 0 or idx == len(remaining_codes):
                    try:
                        if results:
                            pd.DataFrame(results).to_csv(
                                self.checkpoint_file,
                                index=False,
                                encoding='utf-8-sig'
                            )
                            logger.info(f"💾 进度已保存: {len(results)} 只有效股票")
                    except Exception as e:
                        logger.error(f"保存检查点失败: {e}")

        if results:
            df_results = pd.DataFrame(results).drop_duplicates(subset=['股票代码'])
            df_results.to_csv(self.checkpoint_file, index=False, encoding='utf-8-sig')
            logger.info(f"✅ 最终检查点已更新，共 {len(df_results)} 只股票")
        else:
            logger.info("🎯 未筛选到满足条件的股票")

        return results

    # -------------------- 保存最终结果 --------------------
    def save_results(self, results: List[Dict], output_dir: str, timestamp: str):
        if not results:
            logger.info("无满足条件的股票，跳过保存")
            return
        df = pd.DataFrame(results).drop_duplicates(subset=['股票代码'])
        os.makedirs(output_dir, exist_ok=True)
        csv_path = os.path.join(output_dir, f'五线多头选股_{timestamp}.csv')
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        logger.info(f"✅ 最终结果已保存: {csv_path}")

        print("\n满足条件的股票列表（前10）:")
        display_cols = ['股票代码','股票名称','最新收盘','MA5','MA10','10日涨幅(%)','当日成交量','5日均量','10日均量']
        print(df[display_cols].head(10).to_string(index=False))

    # -------------------- 主运行函数 --------------------
    def run(self, csv_path: str, end_date: str = None, code_column: str = 'code', output_dir: str = 'output'):
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')

        print("=" * 80)
        print("五线多头排列选股策略")
        print("=" * 80)
        print(f"输入文件: {csv_path}")
        print(f"分析日期: {end_date}")
        print(f"检查点文件: {self.checkpoint_file}")
        print(f"单只股票整体超时: {self.stock_timeout}秒")
        print(f"使用缓存: {'是' if self.use_cache else '否（将重新开始）'}")
        print("选股条件:")
        print("  1. 五线多头排列: MA5 > MA10 > MA20 > MA30 > MA60")
        print("  2. 收盘价 >= MA10")
        print("  3. 五条均线斜率均 > 0 且 MA5斜率 > MA10斜率 > ... > MA60斜率")
        print(f"  4. 10日涨幅 < {self.max_10d_rise*100:.0f}%")
        print("  5. 当日成交量 > 5日均量 且 > 10日均量")
        print("=" * 80)

        if not self.login():
            logger.error("登录失败，程序退出")
            return

        try:
            df_codes = pd.read_csv(csv_path, encoding='utf-8')
            if code_column not in df_codes.columns:
                code_column = df_codes.columns[0]
                print(f"使用第一列作为股票代码列: {code_column}")
            codes_raw = df_codes[code_column].astype(str).tolist()
            stock_codes = []
            for c in codes_raw:
                c = c.strip()
                if not c.startswith(('sh.', 'sz.', 'bj.')):
                    if c.startswith('6'):
                        c = 'sh.' + c
                    elif c.startswith(('0', '3')):
                        c = 'sz.' + c
                    elif c.startswith(('8', '4')):
                        c = 'bj.' + c
                    else:
                        c = 'sh.' + c
                stock_codes.append(c)

            results = self.batch_analyze(stock_codes, end_date)

            if results:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                self.save_results(results, output_dir, timestamp)
            else:
                logger.info("没有股票同时满足五个条件")

        except Exception as e:
            logger.error(f"运行异常: {traceback.format_exc()}")
        finally:
            self.logout()


# -------------------- 辅助函数 --------------------
def create_sample_csv():
    sample = {
        'code': ['600000', '000001', '600036', '000858', '000333'],
        'name': ['浦发银行', '平安银行', '招商银行', '五粮液', '美的集团']
    }
    df = pd.DataFrame(sample)
    df.to_csv('stock_list.csv', index=False, encoding='utf-8-sig')
    print("✅ 示例CSV已创建: stock_list.csv")


def main():
    print("=" * 80)
    print("五线多头排列选股系统")
    print("=" * 80)
    print("1. 运行批量分析")
    print("2. 创建示例CSV")
    print("3. 退出")
    choice = input("请选择 (1-3): ").strip()
    if choice == '1':
        csv_path = input("请输入CSV文件路径: ").strip()
        if not os.path.exists(csv_path):
            print("❌ 文件不存在")
            return
        end_date = input("请输入分析日期 (YYYY-MM-DD，回车使用今天): ").strip() or None
        code_column = input("请输入股票代码列名 (默认code): ").strip() or 'code'

        use_cache_input = input("是否使用已有缓存继续？ (y/n，默认y): ").strip().lower()
        use_cache = use_cache_input != 'n'

        selector = DailySelector(stock_timeout=8, use_cache=use_cache)   # 修改为 8 秒
        selector.run(csv_path, end_date, code_column)
    elif choice == '2':
        create_sample_csv()
    elif choice == '3':
        print("再见！")
    else:
        print("无效选择")


if __name__ == "__main__":
    main()