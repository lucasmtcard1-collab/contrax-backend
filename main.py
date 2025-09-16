import os
import json
import uuid
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
import stripe
import firebase_admin
from firebase_admin import credentials, firestore
import mercadopago
from fpdf import FPDF

app = FastAPI()

# ==================================================
# 🌐 Rota raiz (teste do backend)
# ==================================================
@app.get("/")
def root():
    return {"message": "API Contrax está rodando, graças a Deus 🚀"}

# ==================================================
# 🔑 Configurações Stripe (pega do Render)
# ==================================================
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

PRICE_BASIC = "price_1S3KUV3hWlIsRkVIVoplem92"
PRICE_STANDARD = "price_1S3KV83hWlIsRkVINxTJ4Cgp"

# ==================================================
# 🔑 Configurações Firebase (pega do Render)
# ==================================================
if not firebase_admin._apps:
    firebase_credentials = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if firebase_credentials:
        cred_dict = json.loads(firebase_credentials)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)

db = firestore.client()

# ==================================================
# 🔑 Configurações Mercado Pago (pega do Render)
# ==================================================
mp = mercadopago.SDK(os.getenv("MERCADOPAGO_ACCESS_TOKEN"))

# ==================================================
# 1️⃣ Stripe - Criar sessão de checkout
# ==================================================
@app.post("/create-checkout-session/")
async def create_checkout_session(request: Request):
    data = await request.json()
    plano = data.get("plano")
    user_id = data.get("userId")

    if plano not in ["basic", "standard"]:
        return {"error": "Plano inválido"}

    price_id = PRICE_BASIC if plano == "basic" else PRICE_STANDARD

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url="contrax://success",
        cancel_url="contrax://cancel",
        metadata={"userId": user_id, "plano": plano},
    )

    return {"id": session.id, "url": session.url}

# ==================================================
# 2️⃣ Stripe - Webhook
# ==================================================
@app.post("/webhook/")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print("❌ Erro no webhook Stripe:", e)
        return {"error": str(e)}

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session["metadata"].get("userId")
        plano = session["metadata"].get("plano")

        if user_id and plano:
            db.collection("usuarios").document(user_id).set({
                "plano": plano,
                "contratosMes": 0,
                "ultimoReset": firestore.SERVER_TIMESTAMP,
                "assinaturaAtiva": True
            }, merge=True)
            print(f"✅ [Stripe] Usuário {user_id} atualizado para plano {plano}")

    return {"status": "success"}

# ==================================================
# 3️⃣ Mercado Pago - Criar checkout
# ==================================================
@app.post("/checkout-mercadopago/")
async def checkout_mercadopago(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    plano = data.get("plano")

    prices = {"basic": 25.00, "standard": 75.00}

    if plano not in prices:
        return {"error": "Plano inválido"}

    preference = {
        "items": [{
            "title": f"Plano {plano.capitalize()}",
            "quantity": 1,
            "currency_id": "BRL",
            "unit_price": prices[plano],
        }],
        "payer": {"email": data.get("email", "teste@teste.com")},
        "metadata": {"userId": user_id, "plano": plano},
        "back_urls": {"success": "contrax://success", "failure": "contrax://cancel"},
        "auto_return": "approved"
    }

    result = mp.preference().create(preference)
    return {"url": result["response"]["init_point"]}

# ==================================================
# 4️⃣ Mercado Pago - Webhook
# ==================================================
@app.post("/webhook-mercadopago/")
async def webhook_mercadopago(request: Request):
    body = await request.json()
    print("📩 Webhook Mercado Pago:", body)

    if "data" in body:
        payment_id = body["data"]["id"]
        payment_info = mp.payment().get(payment_id)

        if payment_info["response"]["status"] == "approved":
            user_id = payment_info["response"]["metadata"]["userId"]
            plano = payment_info["response"]["metadata"]["plano"]

            db.collection("usuarios").document(user_id).set({
                "plano": plano,
                "contratosMes": 0,
                "ultimoReset": firestore.SERVER_TIMESTAMP,
                "assinaturaAtiva": True
            }, merge=True)
            print(f"✅ [MercadoPago] Usuário {user_id} atualizado para plano {plano}")

    return {"status": "ok"}

# ==================================================
# 5️⃣ Contratos - Criação com limite por plano (com reset mensal)
# ==================================================
@app.post("/criar-contrato/")
async def criar_contrato(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    titulo = data.get("titulo")
    conteudo = data.get("conteudo")

    if not user_id or not titulo or not conteudo:
        return {"error": "Faltam campos obrigatórios"}

    user_ref = db.collection("usuarios").document(user_id)
    user_doc = user_ref.get()
    if not user_doc.exists:
        return {"error": "Usuário não encontrado"}

    user_data = user_doc.to_dict()
    plano = user_data.get("plano", "free")
    contratosMes = user_data.get("contratosMes", 0)
    ultimoReset = user_data.get("ultimoReset")
    agora = datetime.utcnow()

    if ultimoReset:
        ultimoReset_dt = ultimoReset.replace(tzinfo=None)
        if ultimoReset_dt.month != agora.month or ultimoReset_dt.year != agora.year:
            contratosMes = 0
            user_ref.update({"contratosMes": 0, "ultimoReset": agora})

    limites = {"free": 1, "basic": 10, "standard": float("inf")}
    limite = limites.get(plano, 1)

    if contratosMes >= limite:
        return {"error": f"Limite de contratos atingido para o plano {plano}"}

    contrato_id = str(uuid.uuid4())
    contrato_data = {
        "id": contrato_id,
        "userId": user_id,
        "titulo": titulo,
        "conteudo": conteudo,
        "status": "rascunho",
        "plano": plano,
        "dataCriacao": agora
    }

    db.collection("contratos").document(contrato_id).set(contrato_data)
    user_ref.update({"contratosMes": contratosMes + 1})

    # 🔹 Registrar log de atividade
    db.collection("atividades").add({
        "userId": user_id,
        "acao": "criou contrato",
        "contratoId": contrato_id,
        "timestamp": agora
    })

    return {"message": "Contrato criado com sucesso", "contrato": contrato_data}

# ==================================================
# 6️⃣ Listar contratos (usuário)
# ==================================================
@app.get("/meus-contratos/{user_id}")
def listar_contratos_usuario(user_id: str):
    contratos = db.collection("contratos").where("userId", "==", user_id).stream()
    return [c.to_dict() for c in contratos]

# ==================================================
# 7️⃣ Listar contratos (admin)
# ==================================================
@app.get("/todos-contratos/")
def listar_contratos_admin():
    contratos = db.collection("contratos").stream()
    return [c.to_dict() for c in contratos]

# ==================================================
# 8️⃣ Download contrato em PDF (marca d’água no Free)
# ==================================================
@app.get("/download-contrato/{contrato_id}")
def download_contrato(contrato_id: str):
    contrato = db.collection("contratos").document(contrato_id).get()
    if not contrato.exists:
        return {"error": "Contrato não encontrado"}

    contrato_data = contrato.to_dict()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.multi_cell(0, 10, contrato_data["conteudo"])

    if contrato_data["plano"] == "free":
        pdf.set_font("Arial", style="B", size=50)
        pdf.set_text_color(200, 200, 200)
        pdf.text(30, 200, "CONTRAX FREE")

    filename = f"{contrato_id}.pdf"
    pdf.output(filename)
    return FileResponse(filename, media_type="application/pdf", filename=filename)

# ==================================================
# 9️⃣ Perfil - Buscar dados
# ==================================================
@app.get("/perfil/{user_id}")
def get_perfil(user_id: str):
    user = db.collection("usuarios").document(user_id).get()
    if not user.exists:
        return {"error": "Usuário não encontrado"}
    return user.to_dict()

# ==================================================
# 🔟 Perfil - Atualizar dados
# ==================================================
@app.post("/perfil/{user_id}")
async def update_perfil(user_id: str, request: Request):
    data = await request.json()
    db.collection("usuarios").document(user_id).set(data, merge=True)
    return {"message": "Perfil atualizado com sucesso"}

# ==================================================
# 1️⃣1️⃣ Assinar contrato
# ==================================================
@app.post("/assinar-contrato/{contrato_id}")
async def assinar_contrato(contrato_id: str):
    contrato_ref = db.collection("contratos").document(contrato_id)
    contrato = contrato_ref.get()
    if not contrato.exists:
        return {"error": "Contrato não encontrado"}

    contrato_ref.update({"status": "assinado"})
    db.collection("atividades").add({
        "userId": contrato.to_dict()["userId"],
        "acao": "assinou contrato",
        "contratoId": contrato_id,
        "timestamp": datetime.utcnow()
    })
    return {"message": "Contrato assinado com sucesso"}

# ==================================================
# 1️⃣2️⃣ Finalizar contrato
# ==================================================
@app.post("/finalizar-contrato/{contrato_id}")
async def finalizar_contrato(contrato_id: str):
    contrato_ref = db.collection("contratos").document(contrato_id)
    contrato = contrato_ref.get()
    if not contrato.exists:
        return {"error": "Contrato não encontrado"}

    contrato_ref.update({"status": "finalizado"})
    db.collection("atividades").add({
        "userId": contrato.to_dict()["userId"],
        "acao": "finalizou contrato",
        "contratoId": contrato_id,
        "timestamp": datetime.utcnow()
    })
    return {"message": "Contrato finalizado com sucesso"}

# ==================================================
# 1️⃣3️⃣ Cancelar contrato
# ==================================================
@app.post("/cancelar-contrato/{contrato_id}")
async def cancelar_contrato(contrato_id: str):
    contrato_ref = db.collection("contratos").document(contrato_id)
    contrato = contrato_ref.get()
    if not contrato.exists:
        return {"error": "Contrato não encontrado"}

    contrato_ref.update({"status": "cancelado"})
    db.collection("atividades").add({
        "userId": contrato.to_dict()["userId"],
        "acao": "cancelou contrato",
        "contratoId": contrato_id,
        "timestamp": datetime.utcnow()
    })
    return {"message": "Contrato cancelado com sucesso"}

# ==================================================
# 1️⃣4️⃣ Dashboard usuário
# ==================================================
@app.get("/dashboard/{user_id}")
def dashboard_usuario(user_id: str):
    contratos = db.collection("contratos").where("userId", "==", user_id).stream()
    total = 0
    assinados = 0
    finalizados = 0
    for c in contratos:
        total += 1
        status = c.to_dict()["status"]
        if status == "assinado":
            assinados += 1
        if status == "finalizado":
            finalizados += 1
    return {"total": total, "assinados": assinados, "finalizados": finalizados}

# ==================================================
# 1️⃣5️⃣ Dashboard admin
# ==================================================
@app.get("/dashboard-admin/")
def dashboard_admin():
    contratos = db.collection("contratos").stream()
    total = 0
    assinados = 0
    finalizados = 0
    for c in contratos:
        total += 1
        status = c.to_dict()["status"]
        if status == "assinado":
            assinados += 1
        if status == "finalizado":
            finalizados += 1
    return {"total": total, "assinados": assinados, "finalizados": finalizados}
