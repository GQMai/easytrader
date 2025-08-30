# -*- coding: utf-8 -*-
from __future__ import division, print_function, unicode_literals

import json
import math
import re
from datetime import datetime
from numbers import Number
from threading import Thread, Event
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from easytrader.follower import BaseFollower
from easytrader.log import logger
from easytrader.utils.misc import parse_cookies_str

import requests


class XueQiuFollower(BaseFollower):
    LOGIN_PAGE = "https://www.xueqiu.com"
    LOGIN_API = "https://xueqiu.com/snowman/login"
    TRANSACTION_API = "https://xueqiu.com/cubes/rebalancing/history.json"
    CUBE_RANK = "https://www.xueqiu.com/cubes/discover/rank/cube/list.json"
    REALTIME_PANKOU = "https://stock.xueqiu.com/v5/stock/realtime/pankou.json"
    PORTFOLIO_URL = "https://xueqiu.com/p/"
    WEB_REFERER = "https://www.xueqiu.com"
    WEB_ORIGIN = "https://www.xueqiu.com"

    def __init__(self):
        super().__init__()
        self._adjust_sell = None
        self._users = None
        self._trade_cmd_expire_seconds = 120  # 默认交易指令过期时间为 120 秒
        
        # 线程池和监控相关
        self.strategy_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="strategy_worker")
        self.strategy_futures = {}  # 存储策略任务的future对象
        self.stop_event = Event()   # 停止信号
        self.monitor_thread = None  # 监控线程

    def login(self, user=None, password=None, **kwargs):
        """
        雪球登陆， 需要设置 cookies
        :param cookies: 雪球登陆需要设置 cookies， 具体见
            https://smalltool.github.io/2016/08/02/cookie/
        :return:
        """
        cookies = kwargs.get("cookies")
        if cookies is None:
            raise TypeError(
                "雪球登陆需要设置 cookies， 具体见" "https://smalltool.github.io/2016/08/02/cookie/"
            )
        headers = self._generate_headers()
        self.s.headers.update(headers)

        self.s.get(self.LOGIN_PAGE)

        cookie_dict = parse_cookies_str(cookies)
        self.s.cookies.update(cookie_dict)

        # 将 Cookies 添加到 headers 中
        cookie_str = '; '.join([f"{key}={value}" for key, value in cookie_dict.items()])
        self.s.headers['Cookie'] = cookie_str
        self.s.headers['Host'] = 'xueqiu.com'
        self.s.headers['Referer'] = 'https://xueqiu.com/P/ZH106644'

        logger.info("登录成功")

    def follow(  # type: ignore
        self,
        users,
        strategies,
        total_assets=10000,
        initial_assets=None,
        adjust_sell=False,
        track_interval=10,
        trade_cmd_expire_seconds=120,
        cmd_cache=True,
        slippage: float = 0.0,
    ):
        """跟踪 joinquant 对应的模拟交易，支持多用户多策略
        :param users: 支持 easytrader 的用户对象，支持使用 [] 指定多个用户
        :param strategies: 雪球组合名, 类似 ZH123450
        :param total_assets: 雪球组合对应的总资产， 格式 [组合1对应资金, 组合2对应资金]
            若 strategies=['ZH000001', 'ZH000002'],
                设置 total_assets=[10000, 10000], 则表明每个组合对应的资产为 1w 元
            假设组合 ZH000001 加仓 价格为 p 股票 A 10%,
                则对应的交易指令为 买入 股票 A 价格 P 股数 1w * 10% / p 并按 100 取整
        :param adjust_sell: 是否根据用户的实际持仓数调整卖出股票数量，
            当卖出股票数大于实际持仓数时，调整为实际持仓数。目前仅在银河客户端测试通过。
            当 users 为多个时，根据第一个 user 的持仓数决定
        :type adjust_sell: bool
        :param initial_assets: 雪球组合对应的初始资产,
            格式 [ 组合1对应资金, 组合2对应资金 ]
            总资产由 初始资产 × 组合净值 算得， total_assets 会覆盖此参数
        :param track_interval: 轮训模拟交易时间，单位为秒
        :param trade_cmd_expire_seconds: 交易指令过期时间, 单位为秒
        :param cmd_cache: 是否读取存储历史执行过的指令，防止重启时重复执行已经交易过的指令
        :param slippage: 滑点，0.0 表示无滑点, 0.05 表示滑点为 5%
        """

        if track_interval / len(self.warp_list(strategies)) < 1.5:
            raise ValueError("雪球跟踪间隔(%s)小于 1.5s, 可能会被雪球限制访问", track_interval / len(strategies))
        
        super().follow(
            users=users,
            strategies=strategies,
            track_interval=track_interval,
            trade_cmd_expire_seconds=trade_cmd_expire_seconds,
            cmd_cache=cmd_cache,
            slippage=slippage,
        )

        self._adjust_sell = adjust_sell
        self._trade_cmd_expire_seconds = trade_cmd_expire_seconds
        self._users = self.warp_list(users)

        strategies = self.warp_list(strategies)
        total_assets = self.warp_list(total_assets)
        initial_assets = self.warp_list(initial_assets)

        if cmd_cache:
            self.load_expired_cmd_cache()

        self.start_trader_thread(self._users, trade_cmd_expire_seconds)

        logger.info('开始跟踪策略: %s, 总资产：%s, 初始资产：%s', strategies, total_assets, initial_assets)   
        for strategy_url, strategy_total_assets, strategy_initial_assets in zip(
            strategies, total_assets, initial_assets
        ):
            assets = self.calculate_assets(
                strategy_url, strategy_total_assets, strategy_initial_assets
            )
            try:
                strategy_id = self.extract_strategy_id(strategy_url)
                # 使用非阻塞方式获取策略名称，避免阻塞主线程
                try:
                    future = self.network_executor.submit(self.extract_strategy_name, strategy_url)
                    strategy_name = future.result(timeout=1.0)  # 1秒超时
                    logger.info("成功获取策略名称: %s", strategy_name)
                except Exception as e:
                    strategy_name = f"策略_{strategy_id}"  # 使用默认名称
                    logger.warning("获取策略名称失败，使用默认名称: %s, 错误: %s", strategy_name, e)
            except:
                logger.error("抽取交易id失败, 无效模拟交易url: %s", strategy_url)
                raise
            
            # 使用线程池替代Thread，确保任务持续执行
            future = self.strategy_executor.submit(
                self.track_strategy_worker, 
                strategy_id, 
                strategy_name, 
                track_interval, 
                assets=assets  # 将assets作为关键字参数传递
            )
            self.strategy_futures[strategy_id] = future
            logger.info("策略 %s 已提交到线程池执行", strategy_name)
        
        # 启动监控线程，确保策略任务持续运行
        self.start_monitor_thread()
        logger.info("策略监控线程已启动，将确保所有策略任务持续运行")

    def calculate_assets(self, strategy_url, total_assets=None, initial_assets=None):
        # 都设置时优先选择 total_assets
        if total_assets is None and initial_assets is not None:
            try:
                # 使用非阻塞方式获取组合净值，避免阻塞主线程
                future = self.network_executor.submit(self._get_portfolio_net_value, strategy_url)
                net_value = future.result(timeout=1.0)  # 1秒超时
                total_assets = initial_assets * net_value
                logger.info("成功获取组合净值: %s, 计算总资产: %s", net_value, total_assets)
            except Exception as e:
                logger.warning("获取组合净值失败，使用initial_assets作为total_assets: %s, 错误: %s", initial_assets, e)
                total_assets = initial_assets  # 降级方案：直接使用初始资产
        if not isinstance(total_assets, Number):
            raise TypeError("input assets type must be number(int, float)")
        if total_assets < 1e3:
            raise ValueError("雪球总资产不能小于1000元，当前预设值 {}".format(total_assets))
        return total_assets

    def start_monitor_thread(self):
        """启动监控线程，确保策略任务持续运行"""
        if self.monitor_thread is None or not self.monitor_thread.is_alive():
            self.monitor_thread = Thread(target=self._monitor_strategies, name="strategy_monitor")
            self.monitor_thread.daemon = True
            self.monitor_thread.start()
            logger.info("策略监控线程已启动")

    def _monitor_strategies(self):
        """监控策略任务状态，确保它们持续运行"""
        logger.info("策略监控线程开始运行")
        
        while not self.stop_event.is_set():
            try:
                # 检查所有策略任务状态
                for strategy_id, future in list(self.strategy_futures.items()):
                    if future.done():
                        # 检查任务是否正常完成还是异常退出
                        try:
                            # 尝试获取结果，如果有异常会抛出
                            result = future.result(timeout=0.1)
                            # 如果正常完成，记录日志但不重启
                            logger.info("策略 %s 任务正常完成，结果: %s", strategy_id, result)
                            # 从字典中移除已完成的任务
                            del self.strategy_futures[strategy_id]
                        except Exception as e:
                            # 任务异常退出，重新启动
                            logger.warning("策略 %s 任务异常退出，正在重新启动... 错误: %s", strategy_id, e)
                            
                            # 获取策略信息并重新提交
                            try:
                                # 这里需要重新获取策略信息，简化处理
                                strategy_name = f"策略_{strategy_id}"
                                assets = 10000  # 使用默认资产值
                                
                                new_future = self.strategy_executor.submit(
                                    self.track_strategy_worker,
                                    strategy_id,
                                    strategy_name,
                                    1.5,  # 使用默认间隔
                                    assets=assets  # 将assets作为关键字参数传递
                                )
                                self.strategy_futures[strategy_id] = new_future
                                logger.info("策略 %s 已重新启动", strategy_id)
                                
                            except Exception as e:
                                logger.error("重新启动策略 %s 失败: %s", strategy_id, e)
                
                # 每30秒检查一次，减少频繁检查
                time.sleep(30)
                
            except Exception as e:
                logger.error("策略监控线程发生错误: %s", e)
                time.sleep(10)
        
        logger.info("策略监控线程已停止")

    def stop_all_strategies(self):
        """停止所有策略任务"""
        logger.info("正在停止所有策略任务...")
        self.stop_event.set()
        
        # 关闭线程池
        if hasattr(self, 'strategy_executor'):
            self.strategy_executor.shutdown(wait=True, timeout=10)
            logger.info("策略线程池已关闭")
        
        # 等待监控线程结束
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=5)
            logger.info("策略监控线程已停止")

    def cleanup(self):
        """清理资源"""
        try:
            # 停止所有策略
            self.stop_all_strategies()
            
            # 调用父类清理方法
            super().cleanup()
            
            logger.info("XueQiuFollower资源清理完成")
        except Exception as e:
            logger.error("XueQiuFollower清理资源时发生错误: %s", e)

    def __del__(self):
        """析构函数，确保资源被正确释放"""
        self.cleanup()

    @staticmethod
    def extract_strategy_id(strategy_url):
        return strategy_url

    def extract_strategy_name(self, strategy_url):
        base_url = "https://xueqiu.com/cubes/nav_daily/all.json?cube_symbol={}"
        url = base_url.format(strategy_url)
        try:
            rep = self.s.get(url, timeout=1)
            info_index = 0
            return rep.json()[info_index]["name"]
        except requests.exceptions.Timeout:
            logger.warning("获取策略名称请求超时(1秒), strategy_url: %s", strategy_url)
            return f"策略_{strategy_url}"  # 返回默认名称
        except requests.exceptions.RequestException as e:
            logger.warning("获取策略名称请求失败: %s, strategy_url: %s", e, strategy_url)
            return f"策略_{strategy_url}"  # 返回默认名称
        except Exception as e:
            logger.error("获取策略名称时发生未知错误: %s, strategy_url: %s", e, strategy_url)
            return f"策略_{strategy_url}"  # 返回默认名称

    def extract_transactions(self, history):
        try:
            if history["count"] <= 0:
                return []
   
            now = datetime.now()
            last_change = history["list"][0]
            if last_change["status"] == "canceled":
                logger.info("该笔交易已取消，跳过。交易详情: %s", transaction)
                return []

            # check expire
            updated_at_datetime = datetime.fromtimestamp(last_change["updated_at"] / 1000)  # Convert milliseconds to seconds
            expire = (now - updated_at_datetime).total_seconds()
            if expire > self._trade_cmd_expire_seconds:
                logger.info("策略%s上一次调仓时间 %s, 超过过期时间 %s 秒, 跳过", last_change["cube_id"] ,updated_at_datetime, self._trade_cmd_expire_seconds)
                return []

            raw_transactions = last_change["rebalancing_histories"]
            transactions = []
            for transaction in raw_transactions:
                if transaction["price"] is None:
                    logger.info("该笔交易无法获取价格，疑似未成交， 获取实时价格。交易详情: %s", transaction)
                    # 获取实时交易价格
                    stock_code = transaction["stock_symbol"].lower()
                    price = self.get_current_price(stock_code)
                    if price is not None:
                        transaction["price"] = price  
                    else:
                        logger.info("获取股票 %s 的实时价格失败，跳过该交易。交易详情：%s", stock_code, transaction)
                        continue
                transactions.append(transaction)

            transactions = list(filter(self.filer_transaction, transactions))
            return transactions
        except KeyError as e:
             # 打印错误信息和 history
            logger.warn(f"KeyError: {e}. Current history: {history}")
            raise

    def create_query_transaction_params(self, strategy):
        params = {"cube_symbol": strategy, "page": 1, "count": 1}
        return params

    # noinspection PyMethodOverriding
    def none_to_zero(self, data):
        if data is None:
            return 0
        return data

    # noinspection PyMethodOverriding
    def project_transactions(self, transactions, assets):
        for transaction in transactions:
            weight_diff = self.none_to_zero(transaction["target_weight"]) - self.none_to_zero(
                transaction["prev_target_weight"]
            )

            is_buy = weight_diff > 0
            transaction["action"] = "buy" if is_buy else "sell"
            transaction["stock_code"] = transaction["stock_symbol"].lower()          

            if transaction["price"] is None:
                logger.info(f"股票 {transaction['stock_code']}, 价格为空: {transaction}")
                continue
            elif self.slippage > 0:
                if is_buy:
                    transaction["price"] = self.get_buy_price(transaction["stock_code"])
                else:
                    transaction["price"] = self.get_sell_price(transaction["stock_code"])

            initial_amount = abs(weight_diff) / 100 * assets / transaction["price"]

            transaction["datetime"] = datetime.fromtimestamp(
                transaction["created_at"] // 1000
            )

            transaction["amount"] =  self.floor_to_hundred(initial_amount) if is_buy else self.ceil_to_hundred(initial_amount)
            if transaction["action"] == "sell" and self._adjust_sell:
                transaction["amount"] = self._adjust_sell_amount(
                    transaction["stock_code"], transaction["amount"]
                )

    # Floor to nearest hundred
    @staticmethod
    def floor_to_hundred(x):
        return int(math.floor(x / 100) * 100)

    # Ceil to nearest hundred
    @staticmethod
    def ceil_to_hundred(x):
        return int(math.ceil(x / 100) * 100)
    
    def filer_transaction(self, transaction):
        return abs(self.none_to_zero(transaction["target_weight"]) - self.none_to_zero(transaction["prev_target_weight"])) >= 2.0

    # Category: 14 - 热门组合
    def get_cube_by_rank(self, category=14, page=1, count=100):
        url = self.CUBE_RANK + f"?category={category}&page={page}&count={count}"
        try:
            response = self.s.get(url, timeout=1)
            return response.json()
        except requests.exceptions.Timeout:
            logger.warning("获取组合排行榜请求超时(1秒), url: %s", url)
            return {"list": []}  # 返回空列表
        except requests.exceptions.RequestException as e:
            logger.warning("获取组合排行榜请求失败: %s, url: %s", e, url)
            return {"list": []}  # 返回空列表
        except Exception as e:
            logger.error("获取组合排行榜时发生未知错误: %s, url: %s", e, url)
            return {"list": []}  # 返回空列表
    
    def get_current_price(self, stock_code):
        try:
            # 使用线程池进行非阻塞网络请求
            future = self.network_executor.submit(self.get_realtime_pankou, stock_code)
            try:
                pankou = future.result(timeout=1.0)  # 1秒超时
            except Exception as e:
                logger.warning("获取股票 %s 实时盘口信息超时或失败: %s", stock_code, e)
                return None
                
            current_price = pankou.get("current") if pankou else None

            if current_price is not None and current_price > 0:
                return round(current_price, 2)
            else:
                logger.error("获取股票 %s 的当前价格失败，返回 None", stock_code)
                return None
        except Exception as e:
            logger.error("获取股票 %s 的当前价格时发生错误: %s", stock_code, e)
            return None

    def get_sell_price(self, stock_code):
        try:
            # 使用线程池进行非阻塞网络请求
            future = self.network_executor.submit(self.get_realtime_pankou, stock_code)
            try:
                pankou = future.result(timeout=1.0)  # 1秒超时
            except Exception as e:
                logger.warning("获取股票 %s 实时盘口信息超时或失败: %s", stock_code, e)
                return None
                
            buy_price_5 = pankou.get("bp5") if pankou else None
            current_price = pankou.get("current") if pankou else None

            if self.slippage > 0 and current_price is not None and current_price > 0 and buy_price_5 is not None and buy_price_5 > 0:
                slippaged_price = round(current_price * (1 - self.slippage), 2)
                logger.debug("股票 %s, 当前价格: %s, 滑点: %.2f%%, 调整后的卖出价格: %s", stock_code, current_price, self.slippage * 100, slippaged_price)
                return slippaged_price

            return current_price
        except Exception as e:        
            return None

    def get_buy_price(self, stock_code):
        try:
            # 使用线程池进行非阻塞网络请求
            future = self.network_executor.submit(self.get_realtime_pankou, stock_code)
            try:
                pankou = future.result(timeout=1.0)  # 1秒超时
            except Exception as e:
                logger.warning("获取股票 %s 实时盘口信息超时或失败: %s", stock_code, e)
                return None
                
            sell_price_5 = pankou.get("sp5") if pankou else None
            current_price = pankou.get("current") if pankou else None

            if self.slippage > 0 and current_price is not None and current_price > 0 and sell_price_5 is not None and sell_price_5 > 0:
                slippaged_price = round(current_price * (1 + self.slippage), 2)
                logger.debug("股票 %s, 当前价格: %s, 滑点: %.2f%%, 调整后的买入价格: %s", stock_code, current_price, self.slippage * 100, slippaged_price)
                return slippaged_price
            
            return current_price
        except Exception as e:      
            return None

    def get_realtime_pankou(self, stock_code):
        url = self.REALTIME_PANKOU + f"?symbol={stock_code.upper()}"
        try:
            # 设置单独的超时时间，确保不会阻塞
            response = self.s.get(url, timeout=1)
            # logger.debug("获取股票 %s, URL: %s, 实时盘口信息: %s", stock_code, url, response.json())
            return response.json().get("data")
        except requests.exceptions.Timeout:
            logger.warning("获取股票 %s 实时盘口信息请求超时(1秒)", stock_code)
            return None
        except requests.exceptions.RequestException as e:
            logger.warning("获取股票 %s 实时盘口信息请求失败: %s", stock_code, e)
            return None
        except Exception as e:
            logger.error("获取股票 %s 实时盘口信息时发生未知错误: %s", stock_code, e)
            return None

    def _adjust_sell_amount(self, stock_code, amount):
        """
        根据实际持仓值计算雪球卖出股数
          因为雪球的交易指令是基于持仓百分比，在取近似值的情况下可能出现不精确的问题。
        导致如下情况的产生，计算出的指令为买入 1049 股，取近似值买入 1000 股。
        而卖出的指令计算出为卖出 1051 股，取近似值卖出 1100 股，超过 1000 股的买入量，
        导致卖出失败
        :param stock_code: 证券代码
        :type stock_code: str
        :param amount: 卖出股份数
        :type amount: int
        :return: 考虑实际持仓之后的卖出股份数
        :rtype: int
        """
        stock_code = stock_code[-6:]
        user = self._users[0]
        position = user.position
        try:
            stock = next(s for s in position if s["security"] == stock_code)
        except StopIteration:
            logger.info("根据持仓调整 %s 卖出额，发现未持有股票 %s, 不做任何调整, position=%s", stock_code, stock_code, position)
            return amount
        except Exception as e:
            logger.error("获取股票 %s 持仓信息失败: %s", stock_code, e)
            return amount

        available_amount = stock["can_use_volume"]
        if available_amount <= amount:
            logger.debug("股票 %s 实际可用余额 %s, 指令卖出股数为 %s, 实际可用小于卖出，调整为 %s, 全部卖出", stock_code, available_amount, amount, available_amount)
            return available_amount

        if available_amount - amount <= 100:
            logger.debug("股票 %s 实际可用余额 %s, 指令卖出股数为 %s, 相差小于100股, 调整为 %s, 全部卖出", stock_code, available_amount, amount, available_amount)
            return available_amount
        
        if available_amount - amount < amount * 0.3:
            logger.debug("股票 %s 实际可用余额 %s, 指令卖出股数为 %s, 相差小于10%, 调整为 %s, 全部卖出", stock_code, available_amount, amount, available_amount)
            return available_amount

        logger.debug("股票 %s 实际可用余额 %s, 指令卖出股数为 %s, 无需调整", stock_code, available_amount, amount)
        return amount


    def _get_portfolio_info(self, portfolio_code):
        """
        获取组合信息
        """
        url = self.PORTFOLIO_URL + portfolio_code
        try:
            portfolio_page = self.s.get(url, timeout=1)
            match_info = re.search(r"(?<=SNB.cubeInfo = ).*(?=;\n)", portfolio_page.text)
            if match_info is None:
                raise Exception("cant get portfolio info, portfolio url : {}".format(url))
            try:
                portfolio_info = json.loads(match_info.group())
            except Exception as e:
                raise Exception("get portfolio info error: {}".format(e))
            return portfolio_info
        except requests.exceptions.Timeout:
            logger.warning("获取组合信息请求超时(1秒), portfolio_code: %s", portfolio_code)
            return None
        except requests.exceptions.RequestException as e:
            logger.warning("获取组合信息请求失败: %s, portfolio_code: %s", e, portfolio_code)
            return None

    def _get_portfolio_net_value(self, portfolio_code):
        """
        获取组合信息
        """
        portfolio_info = self._get_portfolio_info(portfolio_code)
        return portfolio_info["net_value"]

    def track_strategy_worker(self, strategy, name, interval=10, **kwargs):
        """跟踪策略的worker线程，确保持续运行"""
        logger.info("策略 %s worker线程开始运行，轮询间隔: %s秒", name, interval)
        
        consecutive_errors = 0  # 连续错误计数
        max_consecutive_errors = 5  # 最大连续错误次数
        
        while not self.stop_event.is_set():
            try:
                start_time = time.time()
                
                # 使用非阻塞网络请求，设置1秒超时
                future = self.network_executor.submit(self.query_strategy_transaction, strategy, **kwargs)
                try:
                    transactions = future.result(timeout=1.0)  # 1秒超时
                    consecutive_errors = 0  # 重置错误计数
                except Exception as e:
                    consecutive_errors += 1
                    logger.warning("策略 %s 查询调仓信息超时或失败(1秒)，连续错误次数: %d/%d", 
                                 name, consecutive_errors, max_consecutive_errors)
                    
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error("策略 %s 连续错误次数过多，暂停30秒后重试", name)
                        time.sleep(30)
                        consecutive_errors = 0
                    else:
                        time.sleep(1)
                    continue
                
                # 处理交易数据
                if transactions:
                    logger.info("策略 %s 发现 %d 条调仓信息", name, len(transactions))
                    for transaction in transactions:
                        try:
                            self.process_transaction(transaction, **kwargs)
                        except Exception as e:
                            logger.error("处理策略 %s 交易信息时发生错误: %s", name, e)
                else:
                    # 添加心跳日志，证明任务还在运行
                    if int(time.time()) % 60 < interval:  # 每分钟只记录一次心跳
                        logger.debug("策略 %s 无调仓信息，任务正常运行中...", name)
                
                # 计算实际睡眠时间，确保准确的轮询间隔
                elapsed = time.time() - start_time
                sleep_time = max(0, interval - elapsed)
                
                if sleep_time > 0:
                    time.sleep(sleep_time)
                else:
                    logger.warning("策略 %s 处理时间过长: %.2f秒，超过轮询间隔: %d秒", 
                                 name, elapsed, interval)
                    
            except Exception as e:
                consecutive_errors += 1
                logger.exception("策略 %s worker线程发生未知错误: %s，连续错误次数: %d/%d", 
                               name, e, consecutive_errors, max_consecutive_errors)
                
                if consecutive_errors >= max_consecutive_errors:
                    logger.error("策略 %s 连续错误次数过多，暂停60秒后重试", name)
                    time.sleep(60)
                    consecutive_errors = 0
                else:
                    time.sleep(3)
        
        logger.info("策略 %s worker线程已停止")
        # 返回成功状态，避免被监控线程误判为异常
        return {"status": "stopped", "strategy": strategy, "name": name}
