"""Python 프로젝트: FastAPI·Flask·Django의 URL, 호출, ORM·SQL → 테이블, 템플릿."""

from pathlib import Path

from sasu import db
from sasu.indexer import index_repository, python_module
from sasu.linker_python import resolve_relative
from sasu.parsers.python_parser import parse_python, route_paths
from sasu.trace import trace_url

FASTAPI = {
    "backend/pyproject.toml": '[project]\nname = "shop"\nversion = "1.0.0"\nrequires-python = ">=3.10"\n'
                              'dependencies = ["fastapi>=0.110", "sqlmodel", "psycopg[binary]", "pydantic"]\n',
    "backend/app/__init__.py": "",
    "backend/app/main.py": """
from fastapi import FastAPI
from app.api.main import api_router
from app.core.config import settings

app = FastAPI(title="shop")
app.include_router(api_router, prefix=settings.API_V1_STR)
""",
    "backend/app/api/__init__.py": "",
    "backend/app/api/main.py": """
from fastapi import APIRouter
from app.api.routes import orders

api_router = APIRouter()
api_router.include_router(orders.router)
""",
    "backend/app/api/routes/__init__.py": "",
    "backend/app/api/routes/orders.py": '''
from fastapi import APIRouter
from app import crud
from app.services.order_service import OrderService

router = APIRouter(prefix="/orders")

@router.get("/{order_id}")
def read_order(order_id: int, session):
    """주문을 조회한다"""
    return crud.get_order(session, order_id)

@router.post("/")
def create_order(data, session):
    service = OrderService(session)
    if data.channel == "KIOSK":
        return service.place_kiosk(data)
    return service.place(data)
''',
    "backend/app/services/__init__.py": "",
    "backend/app/services/order_service.py": '''
from app.repositories.order_repository import OrderRepository

class OrderService:
    def __init__(self, session):
        self.repo = OrderRepository(session)

    def place(self, data):
        # TODO 재고 확인 필요
        return self.repo.insert(data)

    def place_kiosk(self, data):
        return self.repo.insert(data)
''',
    "backend/app/repositories/__init__.py": "",
    "backend/app/repositories/order_repository.py": '''
from sqlmodel import text
from app.models import Order

class OrderRepository:
    def __init__(self, session):
        self.session = session

    def insert(self, data):
        self.session.execute(text("INSERT INTO order_history (order_id) VALUES (:id)"), {"id": data.id})
        return Order.objects.create(**data)
''',
    "backend/app/crud.py": '''
from sqlmodel import select
from app.models import Order

def get_order(session, order_id):
    return session.exec(select(Order).where(Order.id == order_id)).first()
''',
    "backend/app/models.py": '''
from sqlmodel import Field, SQLModel

class OrderBase(SQLModel):
    title: str = Field(description="주문 제목")

class Order(OrderBase, table=True):
    __tablename__ = "tb_order"
    id: int = Field(primary_key=True, description="주문 번호")
''',
}

FLASK = {
    "requirements.txt": "flask==3.0\nsqlalchemy\npymysql\n",
    "shop/__init__.py": """
from flask import Flask
from shop.views import bp

app = Flask(__name__)
app.register_blueprint(bp, url_prefix="/shop")
""",
    "shop/views.py": '''
from flask import Blueprint, render_template
from shop.models import Product, db

bp = Blueprint("shop", __name__, url_prefix="/v1")

@bp.route("/products")
def product_list():
    products = db.session.query(Product).all()
    return render_template("shop/products.html", products=products)
''',
    "shop/models.py": '''
from flask_sqlalchemy import SQLAlchemy
db = SQLAlchemy()

class Product(db.Model):
    __tablename__ = "tb_product"
    name = db.Column(db.String(50), comment="상품명")
''',
    "shop/templates/shop/products.html": "<ul>{% for p in products %}<li>{{ p.name }}</li>{% endfor %}</ul>",
}

DJANGO = {
    "manage.py": "import django\n",
    "blog/__init__.py": "",
    "blog/urls.py": """
from django.urls import path
from . import views

urlpatterns = [
    path("posts/", views.post_list, name="post_list"),
    path("posts/<int:pk>/", views.PostDetailView.as_view(), name="post_detail"),
]
""",
    "blog/views.py": '''
from django.shortcuts import render
from django.views import View
from .models import Post

def post_list(request):
    posts = Post.objects.filter(published=True)
    return render(request, "blog/post_list.html", {"posts": posts})

class PostDetailView(View):
    def get(self, request, pk):
        return Post.objects.get(pk=pk)

    def delete(self, request, pk):
        Post.objects.filter(pk=pk).delete()
''',
    "blog/models.py": '''
from django.db import models

class Post(models.Model):
    title = models.CharField("제목", max_length=100)
    published = models.BooleanField(default=False, verbose_name="공개 여부")
''',
    "config/__init__.py": "",
    "config/urls.py": """
from django.urls import include, path

urlpatterns = [
    path("blog/", include("blog.urls")),
]
""",
    "blog/templates/blog/post_list.html": "<h1>posts</h1>",
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


def _urls(conn) -> list[str]:
    return [r["url"] for r in conn.execute("SELECT DISTINCT url FROM url_mappings ORDER BY url")]


def _labels(conn, url: str) -> list[tuple[str, str]]:
    return [(n.kind, n.label) for n in trace_url(conn, url, full=True).walk()]


def test_module_paths_and_relative_imports():
    assert python_module("backend/app/api/routes/orders.py", ["backend"]) == "app.api.routes.orders"
    assert python_module("src/pkg/__init__.py") == "pkg"
    assert resolve_relative(".views", "blog") == "blog.views"
    assert resolve_relative("..core.config", "app.api") == "app.core.config"


def test_parser_models_and_routes():
    parsed = parse_python(FASTAPI["backend/app/models.py"].encode("utf-8"), "app.models")
    order = next(c for c in parsed.classes if c.name == "Order")
    assert order.table_name == "tb_order"
    assert {f.name: f.doc for f in order.fields}["id"] == "주문 번호"
    routes = parse_python(FASTAPI["backend/app/api/routes/orders.py"].encode("utf-8"), "app.api.routes.orders")
    module = routes.classes[0]
    assert module.stereotype == "Controller"
    assert [route_paths(m) for m in module.methods if m.name == "read_order"] == [[("/{order_id}", "router")]]


def test_fastapi_nested_routers_and_layers(tmp_path):
    conn = _index(tmp_path, FASTAPI)
    assert _urls(conn) == ["/{settings.API_V1_STR}/orders", "/{settings.API_V1_STR}/orders/{order_id}"]
    labels = _labels(conn, "/{settings.API_V1_STR}/orders")
    assert ("method", "OrderService.place") in labels  # 지역 변수 service = OrderService(...) → 이름으로 추정
    assert ("method", "OrderRepository.insert") in labels  # self.repo = OrderRepository(...)
    statements = [label for kind, label in labels if kind == "statement"]
    assert any(s.startswith("INSERT SQL backend/app/repositories/order_repository.py") for s in statements)
    assert "INSERT ORM Order" in statements
    read = _labels(conn, "/{settings.API_V1_STR}/orders/{order_id}")
    assert ("method", "crud.get_order") in read  # from app import crud → crud.get_order
    assert ("statement", "SELECT ORM Order") in read
    tables = conn.execute("SELECT DISTINCT table_name FROM statement_tables ORDER BY 1").fetchall()
    assert [t[0] for t in tables] == ["ORDER_HISTORY", "TB_ORDER"]


def test_fastapi_questions_see_python_signals(tmp_path):
    from sasu.analysis.questions import generate_questions

    conn = _index(tmp_path, FASTAPI)
    kinds = {(q.kind, q.target) for q in generate_questions(conn)}
    assert ("note", "OrderService.place") in kinds
    assert ("magic_value", "orders.create_order") in kinds  # data.channel == "KIOSK"


def test_flask_blueprint_prefix_and_template(tmp_path):
    conn = _index(tmp_path, FLASK)
    assert _urls(conn) == ["/shop/v1/products"]
    labels = _labels(conn, "/shop/v1/products")
    assert ("statement", "SELECT ORM Product") in labels
    assert ("view", "shop/templates/shop/products.html") in labels


def test_django_urls_include_and_class_based_views(tmp_path):
    conn = _index(tmp_path, DJANGO)
    assert _urls(conn) == ["/blog/posts", "/blog/posts/<int:pk>"]
    detail = _labels(conn, "/blog/posts/<int:pk>")
    assert ("method", "PostDetailView.get") in detail and ("method", "PostDetailView.delete") in detail
    assert ("statement", "DELETE ORM Post") in detail
    posts = _labels(conn, "/blog/posts")
    assert ("statement", "SELECT ORM Post") in posts
    assert ("view", "blog/templates/blog/post_list.html") in posts
    table = conn.execute("SELECT table_name FROM statement_tables LIMIT 1").fetchone()[0]
    assert table == "BLOG_POST"  # 앱_모델 기본 규칙


def test_python_build_info(tmp_path):
    from sasu.parsers.build_info import read_build_info

    root = tmp_path / "repo"
    (root / "backend").mkdir(parents=True)
    (root / "backend/pyproject.toml").write_text(FASTAPI["backend/pyproject.toml"], encoding="utf-8")
    info = read_build_info(root)
    assert info.project == "shop" and info.python_version == ">=3.10"
    assert info.stack["프레임워크"] == ["fastapi"]
    assert "sqlmodel" in info.stack["데이터 접근"]


def test_lowercase_codes_count_only_in_equality(tmp_path):
    from sasu.analysis.questions import generate_questions

    files = {
        "pyproject.toml": '[project]\nname = "pay"\n',
        "pay/__init__.py": "",
        "pay/service.py": '''
def settle(order):
    if order.status == "partial_refund":
        return 0
    if "hello" in order.memo or order.kind in ("gift_card", "coupon"):
        return 1
    return 2
''',
    }
    conn = _index(tmp_path, files)
    magic = [q.question for q in generate_questions(conn) if q.kind == "magic_value"]
    assert len(magic) == 1 and all(v in magic[0] for v in ("partial_refund", "gift_card", "coupon"))
    assert "hello" not in magic[0]  # "hello" in memo는 부분 문자열 검사라 업무 코드가 아니다


def test_projects_keep_separate_databases(tmp_path, monkeypatch):
    from sasu import config

    monkeypatch.setenv("SASU_HOME", str(tmp_path / "home"))
    assert config.current_project() is None
    assert config.project_name(tmp_path / "my shop!") == "my-shop"
    for name in ("egov", "shop"):
        path = config.project_db(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        db.connect(path).close()
    config.set_current_project("shop")
    assert config.list_projects() == ["egov", "shop"] and config.current_project() == "shop"
    assert config.project_db("shop") == tmp_path / "home" / "projects" / "shop.db"


def test_session_writes_infer_model(tmp_path):
    files = dict(FASTAPI)
    files["backend/app/crud.py"] += '''

def create_order(session, data):
    order = Order.model_validate(data)
    session.add(order)
    session.commit()

def rename_order(session, order: Order, title):
    order.title = title
    session.add(order)

def delete_order(session, order_id):
    order = session.get(Order, order_id)
    session.delete(order)
'''
    conn = _index(tmp_path, files)
    linked = {
        (r["name"], r["full_id"], r["kind"])
        for r in conn.execute(
            """SELECT m.name, s.full_id, s.kind FROM edges e JOIN methods m ON m.id = e.src_id
               JOIN statements s ON s.id = e.dst_id WHERE e.dst_kind = 'statement'"""
        )
    }
    assert ("create_order", "ORM Order", "insert") in linked
    assert ("rename_order", "ORM Order", "update") in linked
    assert ("delete_order", "ORM Order", "delete") in linked


# django-oscar처럼 재사용 앱으로 만든 Django: 추상 모델 + if 안의 실제 모델, 앱 설정의 get_urls(), get_class·get_model
DJANGO_APPS = {
    "manage.py": "",
    "urls.py": """
from django.apps import apps
from django.urls import include, path

urlpatterns = [
    path("", include(apps.get_app_config("shop").urls[0])),
]
""",
    "shop/__init__.py": "",
    "shop/config.py": """
from django.apps import apps
from django.urls import path
from shop.core.application import AppConfigBase

class Shop(AppConfigBase):
    name = "shop"

    def ready(self):
        self.basket_app = apps.get_app_config("basket")

    def get_urls(self):
        urls = [
            path("basket/", self.basket_app.urls),
        ]
        return urls
""",
    "shop/core/__init__.py": "",
    "shop/core/application.py": """
from django.apps import AppConfig

class AppConfigBase(AppConfig):
    def get_urls(self):
        return []
""",
    "shop/apps/__init__.py": "",
    "shop/apps/basket/__init__.py": "",
    "shop/apps/basket/apps.py": """
from django.contrib.auth.decorators import login_required
from django.urls import path
from shop.core.application import AppConfigBase
from shop.core.loading import get_class

class BasketConfig(AppConfigBase):
    label = "basket"
    name = "shop.apps.basket"

    def ready(self):
        self.add_view = get_class("basket.views", "BasketAddView")
        self.saved_view = get_class("basket.views", "SavedView")

    def get_urls(self):
        return [
            path("add/<int:pk>/", self.add_view.as_view(), name="add"),
            path("saved/", login_required(self.saved_view.as_view()), name="saved"),
        ]
""",
    "shop/apps/basket/abstract_models.py": '''
from django.db import models

class AbstractBasket(models.Model):
    """장바구니"""
    status = models.CharField(max_length=10)

    class Meta:
        abstract = True
        app_label = "basket"

    def add_product(self, product, quantity=1):
        """상품을 담는다. 같은 상품이면 수량만 늘린다"""
        line, created = self.lines.get_or_create(product=product)
        self.save()

class AbstractLine(models.Model):
    basket = models.ForeignKey("basket.Basket", on_delete=models.CASCADE, related_name="lines")

    class Meta:
        abstract = True
        app_label = "basket"
''',
    "shop/apps/basket/models.py": """
from shop.apps.basket.abstract_models import AbstractBasket, AbstractLine
from shop.core.loading import is_model_registered

if not is_model_registered("basket", "Basket"):
    class Basket(AbstractBasket):
        pass

if not is_model_registered("basket", "Line"):
    class Line(AbstractLine):
        pass
""",
    "shop/apps/basket/views.py": """
from django.views import generic
from shop.core.loading import get_model

Line = get_model("basket", "Line")

class BasketAddView(generic.FormView):
    def form_valid(self, form):
        self.request.basket.add_product(form.product)
        return super().form_valid(form)

class SavedView(generic.ListView):
    model = Line
    template_name = "basket/saved.html"
""",
    "shop/templates/basket/saved.html": "<h1>saved</h1>",
    "shop/apps/order/__init__.py": "",
    "shop/apps/order/models.py": """
from django.db import models

class Line(models.Model):
    title = models.CharField(max_length=10)
""",
    "shop/apps/order/utils.py": """
from shop.core.loading import get_model

Line = get_model("order", "Line")

def copy_lines():
    return Line.objects.all()
""",
}


def test_parser_django_reusable_app():
    abstract = parse_python(DJANGO_APPS["shop/apps/basket/abstract_models.py"].encode(), "shop.apps.basket.abstract_models")
    basket = next(c for c in abstract.classes if c.name == "AbstractBasket")
    assert basket.table_name is None  # abstract = True는 테이블이 없다
    assert basket.py_info["model"]["app_label"] == "basket"
    line = next(c for c in abstract.classes if c.name == "AbstractLine")
    assert line.py_info["relations"] == {"lines": "basket"}
    models = parse_python(DJANGO_APPS["shop/apps/basket/models.py"].encode(), "shop.apps.basket.models")
    assert [c.name for c in models.classes] == ["models", "Basket", "Line"]  # if not is_model_registered(...) 안
    config = parse_python(DJANGO_APPS["shop/apps/basket/apps.py"].encode(), "shop.apps.basket.apps")
    assert {(u.route, u.target) for u in config.url_patterns} == {
        ("add/<int:pk>/", "self.add_view.as_view()"), ("saved/", "login_required(self.saved_view.as_view())")}
    cls = next(c for c in config.classes if c.name == "BasketConfig")
    assert cls.py_info["attrs"]["label"] == "basket"
    assert cls.py_info["loaded"]["add_view"] == ["get_class", "basket.views", "BasketAddView"]
    views = parse_python(DJANGO_APPS["shop/apps/basket/views.py"].encode(), "shop.apps.basket.views")
    saved = next(c for c in views.classes if c.name == "SavedView")
    assert [m.name for m in saved.methods] == ["<class>"]  # 메서드 없는 클래스 뷰도 URL을 걸 곳이 있다


def test_django_reusable_app_urls_models_and_orm(tmp_path):
    conn = _index(tmp_path, DJANGO_APPS)
    # 루트 urls.py → include(get_app_config("shop")) → Shop.get_urls → self.basket_app.urls → BasketConfig.get_urls
    assert _urls(conn) == ["/basket/add/<int:pk>", "/basket/saved"]
    tables = dict(conn.execute("SELECT name, table_name FROM classes WHERE name IN ('Basket', 'AbstractBasket')").fetchall())
    assert tables == {"Basket": "basket_basket", "AbstractBasket": None}
    add = _labels(conn, "/basket/add/<int:pk>")
    assert ("method", "BasketAddView.form_valid") in add
    assert ("method", "AbstractBasket.add_product") in add  # request.basket → Basket 모델 → 추상 부모의 메서드
    assert ("statement", "INSERT ORM basket.Line") in add  # self.lines.get_or_create → related_name="lines"
    assert ("statement", "UPDATE ORM Basket") in add  # 추상 모델의 self.save() → 실제 모델 테이블
    saved = _labels(conn, "/basket/saved")
    assert ("method", "SavedView.<class>") in saved
    assert ("statement", "SELECT ORM basket.Line") in saved  # model = Line (get_model("basket", "Line"))
    assert ("view", "shop/templates/basket/saved.html") in saved  # template_name
    from sasu.analysis.overview import class_modules

    view_id = conn.execute("SELECT id FROM classes WHERE name = 'BasketAddView'").fetchone()[0]
    assert class_modules(conn)[view_id] == "basket"  # 지도·동작 목록은 Django 앱(apps.py 폴더) 단위
    linked = {(r["name"], r["full_id"]) for r in conn.execute(
        """SELECT m.name, s.full_id FROM edges e JOIN methods m ON m.id = e.src_id
           JOIN statements s ON s.id = e.dst_id WHERE e.dst_kind = 'statement'""")}
    assert ("copy_lines", "ORM order.Line") in linked  # 같은 이름 Line이어도 get_model("order", ...) 쪽
    order_line = conn.execute(
        "SELECT t.table_name FROM statements s JOIN statement_tables t ON t.statement_id = s.id WHERE s.full_id = 'ORM order.Line'"
    ).fetchone()[0]
    assert order_line == "ORDER_LINE"
