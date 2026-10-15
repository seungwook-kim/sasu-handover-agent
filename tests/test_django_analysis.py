"""Django 프로젝트의 인수인계 질문·정리 후보: 시그널·미들웨어·설정값·매니저·get_class·명령·raw SQL·트랜잭션,
URL 이름({% url %})·이름으로 부르는 메서드. Java 결과가 바뀌지 않는지는 기존 테스트가 본다."""

from pathlib import Path

from sasu import db
from sasu.analysis.behaviors import list_behaviors
from sasu.analysis.common import analyze_common
from sasu.analysis.questions import KIND_LABEL, generate_questions, is_code_value
from sasu.analysis.unused import find_unused
from sasu.indexer import index_repository
from sasu.parsers.python_parser import parse_python
from sasu.parsers.web_refs import extract_template_refs

SHOP = {
    "manage.py": "import django\n",
    "shop/__init__.py": "",
    "shop/settings.py": '''
DEBUG = True
MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    "shop.orders.middleware.CartMiddleware",
]
TEMPLATES = [{"OPTIONS": {"context_processors": ["shop.orders.context_processors.cart"]}}]
SHOP_ALLOW_GUEST_CHECKOUT = False
''',
    "shop/urls.py": '''
from django.urls import include, path

urlpatterns = [path("orders/", include("shop.orders.urls"))]
''',
    "shop/loading.py": '''
def get_class(module_label, name):
    return name
''',
    "shop/orders/__init__.py": "",
    "shop/orders/apps.py": '''
from django.apps import AppConfig

class OrdersConfig(AppConfig):
    name = "shop.orders"
''',
    "shop/orders/signals.py": '''
from django.dispatch import Signal

order_placed = Signal()
''',
    "shop/orders/managers.py": '''
from django.db import models

class OpenOrderManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(status="Open")

    def by_owner(self, owner):
        return self.get_queryset().filter(owner=owner)
''',
    "shop/orders/models.py": '''
from django.db import models
from shop.orders.managers import OpenOrderManager

class Order(models.Model):
    status = models.CharField(max_length=10)
    title = models.CharField(max_length=50)
    objects = models.Manager()
    open = OpenOrderManager()

    def _get_total(self):
        return 1

    def _set_total(self, value):
        pass

    total = property(_get_total, _set_total)

    def label_for(self, field):
        if field == "title":
            return self.title
        if self.status == "pending":
            return "대기"
        return ""

    def archive_old(self):
        return Order.objects.extra(where=["created < now()"])

    def summary_text(self):
        return "요약"

    def legacy_export(self):
        return []

    def _render_kind_a(self):
        return "a"
''',
    "shop/orders/receivers.py": '''
from django.db.models.signals import post_save
from django.dispatch import receiver
from shop.orders.models import Order
from shop.orders.signals import order_placed

@receiver(post_save, sender=Order)
def touch_stats(sender, instance, **kwargs):
    pass

@receiver(order_placed)
def send_receipt(sender, order, **kwargs):
    pass

def setup(app):
    app.connect(touch_stats)
''',
    "shop/orders/middleware.py": '''
class CartMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)
''',
    "shop/orders/context_processors.py": '''
def _helper(request):
    return 1

def cart(request):
    return {"cart_size": _helper(request)}
''',
    "shop/orders/views.py": '''
from django.conf import settings
from django.db import connection, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from shop.loading import get_class
from shop.orders import signals
from shop.orders.models import Order

Calculator = get_class("orders.calculators", "Calculator")
Repo = get_class("orders.repository", "Repo")
Form = get_class("orders.forms", "Form")
Table = get_class("dashboard.tables", "Table")
Creator = get_class("orders.utils", "Creator")

class PlaceOrderView(View):
    place_signal = signals.order_placed

    @transaction.atomic
    def post(self, request):
        if not request.user.is_authenticated and not settings.SHOP_ALLOW_GUEST_CHECKOUT:
            return redirect(reverse("orders:list"))
        if settings.DEBUG:
            pass
        order = Order.objects.select_for_update().get(pk=1)
        order.save()
        self.place_signal.send(sender=self, order=order)
        try:
            Order.objects.get(pk=2)
        except Order.DoesNotExist:
            pass
        try:
            order.legacy_sync()
        except:
            pass
        return render(request, "orders/done.html", {"order": order})

def order_list(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM orders_order")
    validator = getattr(Order(), "_render_kind_%s" % "a")
    return render(request, "orders/list.html")

def old_report(request):
    return render(request, "orders/list.html")
''',
    "shop/orders/urls.py": '''
from django.urls import path
from shop.orders import views

app_name = "orders"
urlpatterns = [
    path("", views.order_list, name="list"),
    path("place/", views.PlaceOrderView.as_view(), name="place"),
    path("old-report/", views.old_report, name="old_report"),
]
''',
    "shop/orders/management/__init__.py": "",
    "shop/orders/management/commands/__init__.py": "",
    "shop/orders/management/commands/close_orders.py": '''
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    def handle(self, *args, **options):
        pass
''',
    "shop/orders/tasks.py": '''
from celery import shared_task

@shared_task
def resend_receipts():
    pass
''',
    "shop/templates/orders/list.html": '''
{% extends "base.html" %}
<a href="{% url 'orders:place' %}">주문</a>
{{ order.summary_text }} {% if order.total %}{% endif %}
''',
    "shop/templates/orders/done.html": "<p>완료</p>",
}


def _index(tmp_path: Path, files: dict[str, str]):
    repo = tmp_path / "repo"
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    conn = db.connect(tmp_path / "sasu.db")
    index_repository(conn, repo, dialect="oracle")
    return conn


def test_parser_collects_django_facts():
    source = b'''
from django.conf import settings
from django.dispatch import receiver
from django.urls import path, reverse_lazy

@receiver([post_save, post_delete], sender=models.Order)
def handler(sender, **kwargs):
    pass

class View:
    add_signal = signals.basket_addition
    success_url = reverse_lazy("orders:list")
    list_display = ("title", "get_total")

def f():
    if settings.SHOP_FLAG:
        return settings.SHOP_NAME
    return getattr(settings, "SHOP_LIMIT", 3)

urlpatterns = [path("x/", f, name="x-list")]
'''
    facts = parse_python(source, "shop.views").classes[0].py_info["facts"]
    kinds = {(f["k"], f.get("name") or f.get("value") or f.get("func")) for f in facts}
    receiver = next(f for f in facts if f["k"] == "receiver")
    assert receiver["signals"] == "post_save,post_delete" and receiver["sender"] == "Order"
    assert ("attr", "add_signal") in kinds and ("urlref", "orders:list") in kinds and ("urlname", "x-list") in kinds
    assert ("str", "get_total") in kinds  # 관리자 list_display처럼 이름으로 가리키는 메서드
    settings = {f["name"]: f["cond"] for f in facts if f["k"] == "setting"}
    assert settings == {"SHOP_FLAG": True, "SHOP_NAME": False, "SHOP_LIMIT": False}
    names = next(f for f in facts if f["k"] == "names")["value"].split()
    assert "handler" not in names  # def 이름 자리는 "쓰인 이름"이 아니다


def test_template_refs_for_django():
    refs = extract_template_refs('{% extends "base.html" %}{% url \'basket:summary\' pk=1 %}{{ basket.num_lines|default:"a.b" }}')
    assert ("urlname", "basket:summary") in {(r.kind, r.value) for r in refs}
    assert ("template", "base.html") in {(r.kind, r.value) for r in refs}
    assert {r.value for r in refs if r.kind == "attr"} == {"num_lines"}  # 문자열 안의 점은 속성이 아니다
    assert extract_template_refs("<p>${contextPath}/a.do</p>") == []  # JSP·Thymeleaf에는 손대지 않는다


def test_django_question_kinds(tmp_path):
    conn = _index(tmp_path, SHOP)
    questions = generate_questions(conn)
    by_kind: dict[str, list] = {}
    for q in questions:
        by_kind.setdefault(q.kind, []).append(q)
    for kind in ("signal", "request_hook", "setting_branch", "hidden_filter", "swappable_class", "batch_job",
                 "raw_sql", "transaction"):
        assert kind in by_kind, kind
        assert kind in KIND_LABEL

    signals = {q.target: q for q in by_kind["signal"]}
    assert set(signals) == {"receivers.touch_stats", "receivers.send_receipt"}  # app.connect(...)는 시그널이 아님
    assert "PlaceOrderView.post" in signals["receivers.send_receipt"].why  # self.place_signal.send → order_placed
    assert "PlaceOrderView.post" in signals["receivers.touch_stats"].why  # Order를 저장하는 곳 (post_save, sender=Order)

    hooks = {q.target: q for q in by_kind["request_hook"]}
    assert "settings MIDDLEWARE에 등록됨" in hooks["CartMiddleware.__call__"].why
    assert "context_processors.cart" in hooks and "context_processors._helper" not in hooks

    settings_q = by_kind["setting_branch"]
    assert [q.target for q in settings_q] == ["PlaceOrderView.post"]
    assert "SHOP_ALLOW_GUEST_CHECKOUT(기본값 False" in settings_q[0].question and "DEBUG" not in settings_q[0].question

    hidden = by_kind["hidden_filter"][0]
    assert "Order.open" in hidden.question and 'status="Open"' in hidden.question
    assert "by_owner" not in hidden.question  # 매개변수로 거르는 것은 숨은 조건이 아니다

    assert {q.target for q in by_kind["batch_job"]} == {"Command.handle", "tasks.resend_receipts"}
    assert {q.target for q in by_kind["raw_sql"]} == {"views.order_list", "Order.archive_old"}
    assert "select_for_update" in by_kind["transaction"][0].question or "행 잠금" in by_kind["transaction"][0].question

    catches = {q.question.split("를")[0]: q for q in by_kind["empty_catch"]}
    broad = next(q for q in by_kind["empty_catch"] if "모든 예외" in q.question)
    narrow = catches["Order.DoesNotExist"]
    assert narrow.priority < broad.priority  # "없으면 넘어감"은 모든 예외를 삼키는 것보다 뒤로

    values = {q.question for q in by_kind.get("magic_value", [])}
    assert any("'pending'" in v for v in values) and not any("'title'" in v for v in values)


def test_is_code_value_python_rules_do_not_touch_java():
    assert is_code_value("09", "java", 'sTemp.equals("09")')
    assert is_code_value("100", "java", "x > 100")  # Java 판단은 그대로
    assert not is_code_value("100", "python", "self.value > 100")  # 크기 비교 기준은 코드 값이 아니다
    assert not is_code_value("title", "python", 'field == "title"', {"title"})
    assert not is_code_value("continue", "python", 'action == "continue"')
    assert is_code_value("partial_refund", "python", 'kind == "partial_refund"')


def test_django_unused_urls_by_name_and_methods(tmp_path):
    conn = _index(tmp_path, SHOP)
    report = find_unused(conn)
    # {% url 'orders:place' %}·reverse("orders:list")로 부르는 URL은 쓰는 것, 아무도 이름을 부르지 않는 URL만 후보
    assert [f.name for f in report.url] == ["/orders/old-report"]
    methods = {f.name.split(".")[-1] for f in report.method}
    assert "legacy_export" in methods
    # property(_get_total, ...), 템플릿 {{ order.summary_text }}, "_render_kind_%s", 시그널·명령·작업은 쓰는 것
    assert not methods & {"_get_total", "_set_total", "summary_text", "_render_kind_a", "touch_stats",
                          "send_receipt", "handle", "resend_receipts", "cart"}


def test_common_and_behaviors_use_django_apps(tmp_path):
    conn = _index(tmp_path, SHOP)
    modules = {s.module for s in analyze_common(conn).shared} | {b.module for b in list_behaviors(conn)}
    assert "shop.orders" in modules  # 앱(apps.py가 있는 폴더) 단위
    place = next(b for b in list_behaviors(conn) if b.url == "/orders/place")
    assert "설정 SHOP_ALLOW_GUEST_CHECKOUT" in place.branches
    assert any(e.startswith("시그널 order_placed → receivers.send_receipt") for e in place.external)
