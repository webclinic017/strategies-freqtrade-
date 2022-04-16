import numpy as np
import scipy.fft
from scipy.fft import rfft, irfft
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib
import arrow

from freqtrade.strategy import (IStrategy, merge_informative_pair, stoploss_from_open,
                                IntParameter, DecimalParameter, CategoricalParameter)

from typing import Dict, List, Optional, Tuple, Union
from pandas import DataFrame, Series
from functools import reduce
from datetime import datetime, timedelta
from freqtrade.persistence import Trade

# Get rid of pandas warnings during backtesting
import pandas as pd

pd.options.mode.chained_assignment = None  # default='warn'

# Strategy specific imports, files must reside in same folder as strategy
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

import logging
import warnings

log = logging.getLogger(__name__)
# log.setLevel(logging.DEBUG)
warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)

import custom_indicators as cta

import pywt
import RollingStandardScaler
import RollingDWT


"""
####################################################################################
FBB_DWT2 - use a Discreet Wavelet Transform to estimate future price movements,
          and Fisher/Williams/Bollinger buy/sell signals
          The DWT is good at detecting swings, while the FBB checks are to try and keep
          trades within oversold/overbought regions
          
          This version uses de-trended data. 
          See: https://medium.com/swlh/5-tips-for-working-with-time-series-in-python-d889109e676d

####################################################################################
"""


class FBB_DWT2(IStrategy):
    # Do *not* hyperopt for the roi and stoploss spaces

    # ROI table:
    minimal_roi = {
        "0": 0.1
    }

    # Stoploss:
    stoploss = -0.10

    # Trailing stop:
    trailing_stop = False
    trailing_stop_positive = None
    trailing_stop_positive_offset = 0.0
    trailing_only_offset_is_reached = False

    timeframe = '5m'
    inf_timeframe = '15m'

    use_custom_stoploss = True

    # Recommended
    use_sell_signal = True
    sell_profit_only = False
    ignore_roi_if_buy_signal = True

    # Required
    startup_candle_count: int = 512 # must be power of 2
    process_only_new_candles = True

    custom_trade_info = {}

    ###################################

    # Strategy Specific Variable Storage

    dwt_window = startup_candle_count
    dwt_lookahead = 0

    rolling_scaler = RollingStandardScaler.RollingStandardScaler(window=dwt_window)
    rolling_scaler_inf = RollingStandardScaler.RollingStandardScaler(window=dwt_window)
    rolling_dwt_inf = RollingDWT.RollingDWT(window=dwt_window)
    rolling_dwt = RollingDWT.RollingDWT(window=dwt_window)

    ## Hyperopt Variables

    # FBB_ hyperparams
    buy_bb_gain = DecimalParameter(0.01, 0.50, decimals=2, default=0.09, space='buy', load=True, optimize=True)
    buy_fisher_wr = DecimalParameter(-0.99, -0.75, decimals=2, default=-0.75, space='buy', load=True, optimize=True)
    buy_force_fisher_wr = DecimalParameter(-0.99, -0.85, decimals=2, default=-0.99, space='buy', load=True, optimize=True)

    sell_bb_gain = DecimalParameter(0.7, 1.5, decimals=2, default=0.8, space='sell', load=True, optimize=True)
    sell_fisher_wr = DecimalParameter(0.75, 0.99, decimals=2, default=0.9, space='sell', load=True, optimize=True)
    sell_force_fisher_wr = DecimalParameter(0.85, 0.99, decimals=2, default=0.99, space='sell', load=True, optimize=True)


    # DWT  hyperparams
    buy_dwt_diff = DecimalParameter(0.000, 1.0, decimals=2, default=0.01, space='buy', load=True, optimize=True)
    # buy_dwt_window = IntParameter(8, 164, default=64, space='buy', load=True, optimize=True)
    # buy_dwt_lookahead = IntParameter(0, 64, default=0, space='buy', load=True, optimize=True)

    sell_dwt_diff = DecimalParameter(-1.0, 0.000, decimals=2, default=-0.01, space='sell', load=True, optimize=True)


    # Custom Sell Profit (formerly Dynamic ROI)
    csell_roi_type = CategoricalParameter(['static', 'decay', 'step'], default='step', space='sell', load=True,
                                          optimize=True)
    csell_roi_time = IntParameter(720, 1440, default=720, space='sell', load=True, optimize=True)
    csell_roi_start = DecimalParameter(0.01, 0.05, default=0.01, space='sell', load=True, optimize=True)
    csell_roi_end = DecimalParameter(0.0, 0.01, default=0, space='sell', load=True, optimize=True)
    csell_trend_type = CategoricalParameter(['rmi', 'ssl', 'candle', 'any', 'none'], default='any', space='sell',
                                            load=True, optimize=True)
    csell_pullback = CategoricalParameter([True, False], default=True, space='sell', load=True, optimize=True)
    csell_pullback_amount = DecimalParameter(0.005, 0.03, default=0.01, space='sell', load=True, optimize=True)
    csell_pullback_respect_roi = CategoricalParameter([True, False], default=False, space='sell', load=True,
                                                      optimize=True)
    csell_endtrend_respect_roi = CategoricalParameter([True, False], default=False, space='sell', load=True,
                                                      optimize=True)

    # Custom Stoploss
    cstop_loss_threshold = DecimalParameter(-0.05, -0.01, default=-0.03, space='sell', load=True, optimize=True)
    cstop_bail_how = CategoricalParameter(['roc', 'time', 'any', 'none'], default='none', space='sell', load=True,
                                          optimize=True)
    cstop_bail_roc = DecimalParameter(-5.0, -1.0, default=-3.0, space='sell', load=True, optimize=True)
    cstop_bail_time = IntParameter(60, 1440, default=720, space='sell', load=True, optimize=True)
    cstop_bail_time_trend = CategoricalParameter([True, False], default=True, space='sell', load=True, optimize=True)
    cstop_max_stoploss =  DecimalParameter(-0.30, -0.01, default=-0.10, space='sell', load=True, optimize=True)

    ###################################

    """
    Informative Pair Definitions
    """

    def informative_pairs(self):
        pairs = self.dp.current_whitelist()
        informative_pairs = [(pair, self.inf_timeframe) for pair in pairs]
        return informative_pairs
    
    ###################################

    """
    Indicator Definitions
    """

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:


        # Base pair informative timeframe indicators
        curr_pair = metadata['pair']
        informative = self.dp.get_pair_dataframe(pair=curr_pair, timeframe=self.inf_timeframe)

        # DWT

        # self.rolling_scaler_inf.fit(informative['close'])
        # informative['scaled'] = self.rolling_scaler_inf.transform(informative['close'])

        # self.rolling_dwt_inf.fit(informative['close'])
        informative['dwt_predict'] = self.rolling_dwt_inf.model(informative['close'])
        # informative['dwt_scaled'] = self.dwtModel(informative['scaled'])
        # informative['dwt_predict'] = self.rolling_scaler_inf.inverse_transform(informative['close'])


        # merge into normal timeframe
        dataframe = merge_informative_pair(dataframe, informative, self.timeframe, self.inf_timeframe, ffill=True)

        # calculate predictive indicators in shorter timeframe (not informative)

        self.rolling_scaler.fit(dataframe['close'])
        dataframe['scaled'] = self.rolling_scaler.transform(dataframe['close'])
        # dataframe['returns'] = self.compute_returns(dataframe['scaled'], log=True)

        # dataframe['dwt_predict2'] = self.rolling_dwt.model(dataframe['close'])

        dataframe['dwt_predict'] = dataframe[f"dwt_predict_{self.inf_timeframe}"]

        dataframe['dwt_predict_diff'] = (dataframe['dwt_predict'] - dataframe['scaled']) / 10.0
        # dataframe['dwt_predict_diff2'] = (dataframe['dwt_predict2'] - dataframe['scaled']) / 10.0
        # dataframe['predict_diff2'] = dataframe['predict_diff2'].clip(-5.0, 5.0)


        # FisherBB

        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        rsi = 0.1 * (dataframe['rsi'] - 50)
        dataframe['fisher_rsi'] = (np.exp(2 * rsi) - 1) / (np.exp(2 * rsi) + 1)

        bollinger = qtpylib.bollinger_bands(dataframe['close'], window=20, stds=2)
        dataframe['bb_lowerband'] = bollinger['lower']
        dataframe['bb_middleband'] = bollinger['mid']
        dataframe['bb_upperband'] = bollinger['upper']
        dataframe["bb_gain"] = ((dataframe["bb_upperband"] - dataframe["close"]) / dataframe["close"])

        # Williams %R
        dataframe['wr'] = 0.02 * (self.williams_r(dataframe, period=14) + 50.0)

        # Combined Fisher RSI and Williams %R
        dataframe['fisher_wr'] = (dataframe['wr'] + dataframe['fisher_rsi']) / 2.0


        # Custom Stoploss

        if not metadata['pair'] in self.custom_trade_info:
            self.custom_trade_info[metadata['pair']] = {}
            if not 'had-trend' in self.custom_trade_info[metadata["pair"]]:
                self.custom_trade_info[metadata['pair']]['had-trend'] = False

        # RMI: https://www.tradingview.com/script/kwIt9OgQ-Relative-Momentum-Index/
        dataframe['rmi'] = cta.RMI(dataframe, length=24, mom=5)

        # MA Streak: https://www.tradingview.com/script/Yq1z7cIv-MA-Streak-Can-Show-When-a-Run-Is-Getting-Long-in-the-Tooth/
        dataframe['mastreak'] = cta.mastreak(dataframe, period=4)

        # Trends, Peaks and Crosses
        dataframe['candle-up'] = np.where(dataframe['close'] >= dataframe['open'], 1, 0)
        dataframe['candle-up-trend'] = np.where(dataframe['candle-up'].rolling(5).sum() >= 3, 1, 0)

        dataframe['rmi-up'] = np.where(dataframe['rmi'] >= dataframe['rmi'].shift(), 1, 0)
        dataframe['rmi-up-trend'] = np.where(dataframe['rmi-up'].rolling(5).sum() >= 3, 1, 0)

        dataframe['rmi-dn'] = np.where(dataframe['rmi'] <= dataframe['rmi'].shift(), 1, 0)
        dataframe['rmi-dn-count'] = dataframe['rmi-dn'].rolling(8).sum()

        # Indicators used only for ROI and Custom Stoploss
        ssldown, sslup = cta.SSLChannels_ATR(dataframe, length=21)
        dataframe['sroc'] = cta.SROC(dataframe, roclen=21, emalen=13, smooth=21)
        dataframe['ssl-dir'] = np.where(sslup > ssldown, 'up', 'down')

        return dataframe

    ###################################


    def madev(self, d, axis=None):
        """ Mean absolute deviation of a signal """
        return np.mean(np.absolute(d - np.mean(d, axis)), axis)

    def dwtModel(self, data):

        # the choice of wavelet makes a big difference
        # for an overview, check out: https://www.kaggle.com/theoviel/denoising-with-direct-wavelet-transform
        # wavelet = 'db1'
        # wavelet = 'bior1.1'
        wavelet = 'haar' # deals well with harsh transitions
        level = 1
        wmode = "smooth"
        length = len(data)

        # de-trend the data
        n = data.size
        t = np.arange(0, n)
        p = np.polyfit(t, data, 1)  # find linear trend in data
        x_notrend = data - p[0] * t  # detrended data

        # coeff = pywt.wavedec(x_notrend, wavelet, mode=wmode)
        #
        # # remove higher harmonics
        # sigma = (1 / 0.6745) * self.madev(coeff[-level])
        # uthresh = sigma * np.sqrt(2 * np.log(length))
        # coeff[1:] = (pywt.threshold(i, value=uthresh, mode='hard') for i in coeff[1:])
        #
        # # inverse transform
        # restored_sig = pywt.waverec(coeff, wavelet, mode=wmode)

        (ca, cd) = pywt.dwt(data, wavelet)

        cat = pywt.threshold(ca, np.std(ca) / 2, mode='hard')
        cdt = pywt.threshold(cd, np.std(cd) / 2, mode='hard')

        restored_sig = pywt.idwt(cat, cdt, wavelet)

        # re-trend the data
        ldiff = len(restored_sig) - len(data)
        model = restored_sig[ldiff:] + p[0] * t

        return model

    def model(self, a: np.ndarray) -> np.float:
        #must return scalar, so just calculate prediction and take last value
        model = self.dwtModel(np.array(a))
        length = len(model)
        return model[length-1]

    def predict(self, a: np.ndarray) -> np.float:
        #must return scalar, so just calculate prediction and take last value
        # npredict = self.buy_dwt_lookahead.value
        npredict = self.dwt_lookahead

        y = self.dwtModel(np.array(a))
        length = len(y)
        if npredict == 0:
            predict = y[length-1]
        else:
            x = np.arange(length)
            f = scipy.interpolate.UnivariateSpline(x, y, k=3)

            predict = f(length-1+npredict)

        return predict

    # Williams %R
    def williams_r(self, dataframe: DataFrame, period: int = 14) -> Series:
        """Williams %R, or just %R, is a technical analysis oscillator showing the current closing price in relation to the high and low
            of the past N days (for a given N). It was developed by a publisher and promoter of trading materials, Larry Williams.
            Its purpose is to tell whether a stock or commodity market is trading near the high or the low, or somewhere in between,
            of its recent trading range.
            The oscillator is on a negative scale, from −100 (lowest) up to 0 (highest).
        """

        highest_high = dataframe["high"].rolling(center=False, window=period).max()
        lowest_low = dataframe["low"].rolling(center=False, window=period).min()

        WR = Series(
            (highest_high - dataframe["close"]) / (highest_high - lowest_low),
            name=f"{period} Williams %R",
        )

        return WR * -100

    def compute_returns(self, data, periods=1, log=False, relative=True):
        """Computes returns.

        Calculates the returns of a given dataframe for the given period. The
        returns can be computed as log returns or as arithmetic returns

        Parameters
        ----------
        data : pandas.DataFrame or pandas.Series
            The data to calculate returns of.
        periods : int
            The period difference to compute returns.
        log : bool, optional, default: False
            Whether to compute log returns (True) or not (False).
        relative : bool, optional, default: True
            Whether to compute relative returns (True) or not
            (False).

        Returns
        -------
        ret : pandas.DataFrame or pandas.Series
            The computed returns.

        """
        if log:
            if not relative:
                raise ValueError("Log returns are relative by definition.")
            else:
                ret = self._log_returns(data, periods)
        else:
            ret = self._arithmetic_returns(data, periods, relative)

        return ret

    def _arithmetic_returns(self, data, periods, relative):
        """Arithmetic returns."""
        # to avoid computing it twice
        shifted = data.shift(periods)
        ret = (data - shifted)

        if relative:
            return ret / shifted
        else:
            return ret

    def _log_returns(self, data, periods):
        """Log returns."""
        return np.log(data / data.shift(periods))

    ###################################

    """
    Buy Signal
    """


    def populate_buy_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'buy_tag'] = ''

        # conditions.append(dataframe['volume'] > 0)

        # FFT triggers
        dwt_cond = (
                qtpylib.crossed_above(dataframe['dwt_predict_diff'], self.buy_dwt_diff.value)
        )

        conditions.append(dwt_cond)

        # set buy tags
        dataframe.loc[dwt_cond, 'buy_tag'] += 'dwt_buy '


        # FBB_ triggers
        fbb_cond = (
                (dataframe['fisher_wr'] <= self.buy_fisher_wr.value) &
                (dataframe['bb_gain'] >= self.buy_bb_gain.value)
        )

        strong_buy_cond = (
                (
                        (dataframe['bb_gain'] >= 1.5 * self.buy_bb_gain.value) |
                        (dataframe['fisher_wr'] < self.buy_force_fisher_wr.value)
                ) &
                (
                    (dataframe['bb_gain'] > 0.02)  # make sure there is some potential gain
                )
        )
        conditions.append(fbb_cond | strong_buy_cond)
        dataframe.loc[fbb_cond, 'buy_tag'] += 'fbb_buy '
        dataframe.loc[strong_buy_cond, 'buy_tag'] += 'strong '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'buy'] = 1

        return dataframe


    ###################################

    """
    Sell Signal
    """


    def populate_sell_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        dataframe.loc[:, 'exit_tag'] = ''

        # FFT triggers
        dwt_cond = (
                qtpylib.crossed_below(dataframe['dwt_predict_diff'], self.sell_dwt_diff.value)
        )

        conditions.append(dwt_cond)

        # FBB_ triggers
        fbb_cond = (
                (dataframe['fisher_wr'] > self.sell_fisher_wr.value) &
                (dataframe['close'] >= (dataframe['bb_upperband'] * self.sell_bb_gain.value))
        )

        strong_sell_cond = (
            qtpylib.crossed_above(dataframe['fisher_wr'], self.sell_force_fisher_wr.value) #&
            # (dataframe['close'] > dataframe['bb_upperband'] * self.sell_bb_gain.value)
        )

        conditions.append(fbb_cond | strong_sell_cond)

        # set exit tags
        dataframe.loc[fbb_cond, 'exit_tag'] += 'fbb_sell '
        dataframe.loc[strong_sell_cond, 'exit_tag'] += 'strong_sell '

        # set sell tags
        dataframe.loc[dwt_cond, 'exit_tag'] += 'dwt_sell '

        if conditions:
            dataframe.loc[reduce(lambda x, y: x & y, conditions), 'sell'] = 1

        return dataframe


    ###################################

    """
    Custom Stoploss
    """

    def custom_stoploss(self, pair: str, trade: 'Trade', current_time: datetime, current_rate: float,
                        current_profit: float, **kwargs) -> float:

        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        last_candle = dataframe.iloc[-1].squeeze()
        trade_dur = int((current_time.timestamp() - trade.open_date_utc.timestamp()) // 60)
        in_trend = self.custom_trade_info[trade.pair]['had-trend']

        # limit stoploss
        if current_profit <  self.cstop_max_stoploss.value:
            return 0.01

        # Determine how we sell when we are in a loss
        if current_profit < self.cstop_loss_threshold.value:
            if self.cstop_bail_how.value == 'roc' or self.cstop_bail_how.value == 'any':
                # Dynamic bailout based on rate of change
                if last_candle['sroc'] <= self.cstop_bail_roc.value:
                    return 0.01
            if self.cstop_bail_how.value == 'time' or self.cstop_bail_how.value == 'any':
                # Dynamic bailout based on time, unless time_trend is true and there is a potential reversal
                if trade_dur > self.cstop_bail_time.value:
                    if self.cstop_bail_time_trend.value == True and in_trend == True:
                        return 1
                    else:
                        return 0.01
        return 1

    ###################################

    """
    Custom Sell
    """

    def custom_sell(self, pair: str, trade: 'Trade', current_time: 'datetime', current_rate: float,
                    current_profit: float, **kwargs):

        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        last_candle = dataframe.iloc[-1].squeeze()

        trade_dur = int((current_time.timestamp() - trade.open_date_utc.timestamp()) // 60)
        max_profit = max(0, trade.calc_profit_ratio(trade.max_rate))
        pullback_value = max(0, (max_profit - self.csell_pullback_amount.value))
        in_trend = False

        # Determine our current ROI point based on the defined type
        if self.csell_roi_type.value == 'static':
            min_roi = self.csell_roi_start.value
        elif self.csell_roi_type.value == 'decay':
            min_roi = cta.linear_decay(self.csell_roi_start.value, self.csell_roi_end.value, 0,
                                       self.csell_roi_time.value, trade_dur)
        elif self.csell_roi_type.value == 'step':
            if trade_dur < self.csell_roi_time.value:
                min_roi = self.csell_roi_start.value
            else:
                min_roi = self.csell_roi_end.value

        # Determine if there is a trend
        if self.csell_trend_type.value == 'rmi' or self.csell_trend_type.value == 'any':
            if last_candle['rmi-up-trend'] == 1:
                in_trend = True
        if self.csell_trend_type.value == 'ssl' or self.csell_trend_type.value == 'any':
            if last_candle['ssl-dir'] == 'up':
                in_trend = True
        if self.csell_trend_type.value == 'candle' or self.csell_trend_type.value == 'any':
            if last_candle['candle-up-trend'] == 1:
                in_trend = True

        # Don't sell if we are in a trend unless the pullback threshold is met
        if in_trend == True and current_profit > 0:
            # Record that we were in a trend for this trade/pair for a more useful sell message later
            self.custom_trade_info[trade.pair]['had-trend'] = True
            # If pullback is enabled and profit has pulled back allow a sell, maybe
            if self.csell_pullback.value == True and (current_profit <= pullback_value):
                if self.csell_pullback_respect_roi.value == True and current_profit > min_roi:
                    return 'intrend_pullback_roi'
                elif self.csell_pullback_respect_roi.value == False:
                    if current_profit > min_roi:
                        return 'intrend_pullback_roi'
                    else:
                        return 'intrend_pullback_noroi'
            # We are in a trend and pullback is disabled or has not happened or various criteria were not met, hold
            return None
        # If we are not in a trend, just use the roi value
        elif in_trend == False:
            if self.custom_trade_info[trade.pair]['had-trend']:
                if current_profit > min_roi:
                    self.custom_trade_info[trade.pair]['had-trend'] = False
                    return 'trend_roi'
                elif self.csell_endtrend_respect_roi.value == False:
                    self.custom_trade_info[trade.pair]['had-trend'] = False
                    return 'trend_noroi'
            elif current_profit > min_roi:
                return 'notrend_roi'
        else:
            return None
