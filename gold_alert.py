#!/usr/bin/env python3
"""GOLD CONFIRM v4: read-only technical alerts for GitHub Actions.

Uses the same XAUUSD M5/M15/M30 EMA/Stochastic rules as the app.
No broker connection. No MT5 order placement.
"""
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = 'https://biquote.io/api/XAUUSD'
FRAMES = [('M5', '5m', 5), ('M15', '15m', 15), ('M30', '30m', 30)]
STATE_PATH = Path('.github/gold_signal_state.json')


def now_utc():
    return datetime.now(timezone.utc)


def iso_dt(value):
    if not isinstance(value, str):
        raise ValueError('Tarikh data tidak sah')
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def fetch_json(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'gold-confirm-telegram/1.0',
        'Accept': 'application/json',
        'Cache-Control': 'no-cache',
    })
    with urllib.request.urlopen(req, timeout=18) as response:
        return json.load(response)


def ema(values, period):
    if len(values) < period:
        raise ValueError('EMA: candle tidak cukup')
    out = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for value in values[period:]:
        out = (value - out) * alpha + out
    return out


def stochastic(bars, lookback=14, ksmooth=3, dsmooth=3):
    raw = []
    for i in range(lookback - 1, len(bars)):
        chunk = bars[i - lookback + 1:i + 1]
        lo = min(b['low'] for b in chunk)
        hi = max(b['high'] for b in chunk)
        raw.append(100 * (bars[i]['close'] - lo) / (hi - lo) if hi > lo else 50)
    smoothk = [sum(raw[i - ksmooth + 1:i + 1]) / ksmooth
               for i in range(ksmooth - 1, len(raw))]
    smoothd = [sum(smoothk[i - dsmooth + 1:i + 1]) / dsmooth
               for i in range(dsmooth - 1, len(smoothk))]
    if not smoothk or not smoothd:
        raise ValueError('Stochastic: candle tidak cukup')
    return smoothk[-1], smoothd[-1]


def parse_bars(response):
    raw = response.get('bars')
    if not isinstance(raw, list):
        raise ValueError('Format OHLC tidak sah')
    found = {}
    for b in raw:
        if b.get('isOpen'):
            continue
        try:
            stamp = iso_dt(b['openTime'])
            values = [float(b[k]) for k in ('open','high','low','close')]
            if not all(math.isfinite(v) for v in values) or values[1] < values[2]:
                continue
            found[stamp] = dict(time=stamp,open=values[0],high=values[1],low=values[2],close=values[3])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return [found[key] for key in sorted(found)]


def analyze(frame_name, bars):
    if len(bars) < 58:
        raise ValueError(frame_name + ': jumlah candle kurang daripada 58')
    closes = [b['close'] for b in bars]
    last, prev = bars[-1], bars[-2]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e20prev = ema(closes[:-3], 20)  # Pariti dengan kod aplikasi v4
    k, d = stochastic(bars)
    base_buy = e20 > e50 and e20 > e20prev and last['close'] > e20
    base_sell = e20 < e50 and e20 < e20prev and last['close'] < e20
    momentum_buy = k > d and k < 95
    momentum_sell = k < d and k > 5
    is_m5 = frame_name == 'M5'
    buy = base_buy and momentum_buy and last['close'] > prev['close'] and (
        not is_m5 or last['close'] > prev['high'])
    sell = base_sell and momentum_sell and last['close'] < prev['close'] and (
        not is_m5 or last['close'] < prev['low'])
    return ('BUY' if buy else 'SELL' if sell else 'TUNGGU'), last['time']


def quote_valid(quote, current):
    if quote.get('marketState') == 'closed' or quote.get('stale') is True:
        return False
    try:
        mid = float(quote.get('mid'))
        if not math.isfinite(mid):
            return False
        age_seconds = quote.get('quoteAgeSeconds')
        if age_seconds is not None and float(age_seconds) > 300:
            return False
        quote_time = iso_dt(quote['timestamp'])
        if abs((current - quote_time).total_seconds()) > 300:
            return False
        return True
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def compute_signal(current=None, fetch=fetch_json):
    current = current or now_utc()
    quote = fetch(BASE + '?allowStale=true')
    if not quote_valid(quote, current):
        return None, 'Harga lapuk atau pasaran ditutup', None
    directions = {}
    bar_times = {}
    for label, interval, minutes in FRAMES:
        data = fetch(BASE + '/ohlc?' + urllib.parse.urlencode({'interval': interval, 'limit': 130}))
        bars = parse_bars(data)
        direction, bar_time = analyze(label, bars)
        candle_age = (current - bar_time).total_seconds() - minutes * 60
        if candle_age < -120 or candle_age > (minutes + 6) * 60:
            return None, label + ' mempunyai candle lapuk', None
        directions[label] = direction
        bar_times[label] = bar_time
    if all(directions[name] == 'BUY' for name,_,_ in FRAMES):
        signal = 'BUY'
    elif all(directions[name] == 'SELL' for name,_,_ in FRAMES):
        signal = 'SELL'
    else:
        signal = 'TUNGGU'
    return signal, ' | '.join(k+':'+v for k,v in directions.items()), {
        'mid': float(quote['mid']), 'm5_time': bar_times['M5'], 'now': current,
    }


def send_telegram(message):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat_id:
        raise RuntimeError('GitHub Secrets TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID tiada')
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': message,
                                   'disable_web_page_preview': 'true'}).encode('utf-8')
    req = urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',
                                 data=data, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            output = json.load(response)
            if not output.get('ok'):
                raise RuntimeError('Telegram menolak mesej')
    except urllib.error.HTTPError as exc:
        # Never print exception URL, which embeds the bot token.
        raise RuntimeError('Telegram tidak menerima mesej (HTTP '+str(exc.code)+')') from None
    except urllib.error.URLError:
        raise RuntimeError('Sambungan Telegram gagal') from None


def load_state():
    if not STATE_PATH.exists():
        return {'signal':'TUNGGU'}
    try:
        obj = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        if obj.get('signal') in ('BUY','SELL','TUNGGU'):
            return obj
    except (ValueError,OSError,AttributeError):
        pass
    raise RuntimeError('Fail state rosak; semak .github/gold_signal_state.json')


def save_state(signal, current):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {'signal':signal,'checked_at_utc':current.isoformat(timespec='seconds')}
    STATE_PATH.write_text(json.dumps(state, indent=2)+'\n', encoding='utf-8')


def run():
    if '--test' in sys.argv:
        send_telegram('✅ UJIAN GOLD CONFIRM BERJAYA. Bot Telegram sudah bersambung. Ini BUKAN signal trading.')
        print('Mesej ujian Telegram telah dihantar')
        return
    signal, info, details = compute_signal()
    print('Keputusan XAUUSD:', signal or 'DATA TIDAK SAH', '-', info)
    if signal is None:
        return  # Data tak sah: jangan ubah state atau hantar signal.
    old = load_state().get('signal')
    if signal == old:
        print('Tiada perubahan signal; tiada notifikasi baharu')
        return
    if signal in ('BUY','SELL'):
        stamp = details['now'].astimezone(ZoneInfo('Asia/Kuala_Lumpur')).strftime('%Y-%m-%d %H:%M MYT')
        action = '🟢 BUY' if signal == 'BUY' else '🔴 SELL'
        msg = (f'{action} — PENGESAHAN TEKNIKAL XAUUSD\n'
               'M5, M15 dan M30 searah.\n'
               f'Harga rujukan Biquote: USD {details["mid"]:.2f}\n'
               f'Masa: {stamp}\n\n'
               '⚠️ Ini signal eksperimen, bukan jaminan untung. Harga mungkin berbeza daripada broker MT5.')
        send_telegram(msg)   # State hanya berubah setelah Telegram berjaya menerima mesej.
        print('Notifikasi arah', signal, 'telah dihantar')
    save_state(signal, details['now'])
    print('Keadaan signal disimpan:', signal)


if __name__ == '__main__':
    try:
        run()
    except Exception as exc:
        # API token may appear in urllib errors. Do not print details of unknown network exceptions.
        if isinstance(exc, RuntimeError):
            print('Ralat:', exc, file=sys.stderr)
        else:
            print('Jenis ralat:', type(exc).__name__, file=sys.stderr)
            
        else:
            print('Jenis ralat:', type(exc).__name__, file=sys.stderr)
        sys.exit(1)
