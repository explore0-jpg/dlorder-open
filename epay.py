import json
import random
import time
from datetime import date

import requests


class EpayError(Exception):
    """业务层错误（code 非 200）"""

    def __init__(self, code, msg, data=None):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.data = data or {}


class AuthError(EpayError):
    """未登录 / 帐号异常"""


def trunc_bytes(s, limit=50):
    """按字节数截断（服务端按字节校验，中文 3 字节/字），保证 ≤limit 字节。"""
    if not s:
        return ""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    out = b[:limit]
    while True:
        try:
            return out.decode("utf-8")
        except UnicodeDecodeError:
            out = out[:-1]


class EpayAPI:
    def __init__(self, client_uuid, access_token, mer_id, mer_salt,
                 base_url="https://www.epay100.cn/901/6126/server/index.php",
                 uni_id="6126", attach="5.6.2607315", timeout=10,
                 max_retries=3):
        self.client_uuid = client_uuid
        self.access_token = access_token
        self.mer_id = mer_id
        self.mer_salt = mer_salt
        self.base_url = base_url
        self.uni_id = uni_id
        self.attach = attach
        self.timeout = timeout
        self.max_retries = max_retries

    def _rand(self):
        return f"{int(time.time() * 1000)}-{random.randint(10000000, 99999999)}"

    def call(self, method, data=None, retries=None):
        if retries is None:
            retries = self.max_retries
        url = (
            f"{self.base_url}?mh901_method={method}"
            f"&mh901_client_uuid={self.client_uuid}&mh901_rand={self._rand()}"
        )
        headers = {
            "Authorization": self.access_token,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://www.epay100.cn",
            "Referer": f"https://www.epay100.cn/901/{self.uni_id}/wmall/ShopInfo"
                       f"?mer_id={self.mer_id}&mer_salt={self.mer_salt}",
            "User-Agent": ("Mozilla/5.0 (Linux; Android 14) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0.0.0 Mobile Safari/537.36"),
        }
        payload = {
            "method": method,
            "uni_id": self.uni_id,
            "client_uuid": self.client_uuid,
            "attach": self.attach,
            "data": json.dumps(data or {}, ensure_ascii=False),
        }

        last_err = None
        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(url, headers=headers, data=payload,
                                     timeout=self.timeout)
                resp.raise_for_status()
                body = resp.json()
                code = body.get("code")
                if code in ("C900901", "C900903", "C900904"):
                    raise AuthError(code, body.get("msg", ""), body.get("data"))
                if code != "200":
                    raise EpayError(code, body.get("msg", "未知错误"), body.get("data"))
                return body.get("data", {})
            except (requests.RequestException, ValueError) as e:
                last_err = e
                if attempt < retries:
                    time.sleep(0.3 * attempt)
        raise EpayError("NET", f"请求失败: {last_err}")

    # ---- 基础接口 ----
    def menu(self, day=None):
        """当日/指定日菜品（daily_date 格式 YYYYMMDD，默认今天）。"""
        if day is None:
            day = date.today()
        if isinstance(day, (date,)):
            dd = day.strftime("%Y%m%d")
        else:
            dd = str(day).replace("-", "")
        return self.call("app.shop.getGoodsList", {
            "mer_id": self.mer_id, "mer_salt": self.mer_salt,
            "daily_date": dd,
        })

    def store_info(self):
        return self.call("app.shop.getStoreInfo",
                         {"mer_id": self.mer_id, "mer_salt": self.mer_salt})

    def cart_info(self, goods_mode=None):
        data = {"mer_id": self.mer_id}
        if goods_mode is not None:
            data["goods_mode"] = goods_mode
        return self.call("app.shop.getUserGoodsCartInfo", data)

    def cart_buy(self, goods_id, category_id, goods_title, num=1,
                 goods_mode=2, option=None, option_father_id=None):
        goods_option = []
        if option:
            opt = {"option_name": option, "option_num": num}
            if option_id := getattr(option, "option_id", None):
                opt["option_id"] = option_id
            if category_id:
                opt["option_father_id"] = option_father_id or category_id
            goods_option = [opt]
        data = {
            "mer_id": self.mer_id,
            "category_id": category_id,
            "goods_id": goods_id,
            "goods_title": trunc_bytes(goods_title),
            "goods_total": num,
            "goods_option": goods_option,
            "goods_mode": goods_mode,
        }
        return self.call("app.shop.setUserGoodsCartBuy", data)

    def cart_clear(self):
        return self.call("app.shop.setUserGoodsCartClear", {"mer_id": self.mer_id})

    def cart_confirm(self, goods_mode=2):
        return self.call("app.shop.setUserGoodsCartConfirm",
                         {"mer_id": self.mer_id, "goods_mode": goods_mode})

    def order_submit(self, delivery, goods_mode=2, pay_offline=True,
                     remark="", order_mode=None):
        data = {
            "mer_id": self.mer_id,
            "goods_mode": goods_mode,
            "delivery_name": delivery.get("name", ""),
            "delivery_mobile": delivery.get("mobile", ""),
            "delivery_date": delivery.get("date", ""),
            "delivery_time": delivery.get("time", ""),
            "delivery_time_name": delivery.get("time_name", ""),
            "delivery_remark": remark,
            "is_pay_offline": 1 if pay_offline else 0,
            "delivery_area_L1": delivery.get("area1", ""),
            "delivery_area_L2": delivery.get("area2", ""),
        }
        if order_mode is not None:
            data["order_mode"] = order_mode
        for k in list(data):
            if not data[k]:
                del data[k]
        return self.call("app.shop.setUserOrderSubmit", data)

    def order_pay(self, order_id, order_salt, pay_offline=True, return_url=""):
        return self.call("app.shop.setUserOrderPay", {
            "mer_id": self.mer_id, "mer_salt": self.mer_salt,
            "order_id": order_id, "order_salt": order_salt,
            "is_pay_offline": 1 if pay_offline else 0,
            "return_url": return_url,
        })