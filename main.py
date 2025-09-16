import os
import json
from fastapi import FastAPI, Request
import stripe
import firebase_admin
from firebase_admin import credentials, firestore
import mercadopago

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

# ⚠️ IDs reais dos preços criados no Stripe Dashboard
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
    plano = data.get("plano")      # "basic" ou "standard"
    user_id = data.get("userId")   # vindo do app Flutter

    if plano not in ["basic", "standard"]:
        return {"error": "Plano inválido"}

    price_id = PRICE_BASIC if plano == "basic" else PRICE_STANDARD

    # 🔹 Cria sessão de assinatura no Stripe
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],  # pode ativar PIX no dashboard
        line_items=[{
            "price": price_id,
            "quantity": 1,
        }],
        mode="subscription",
        # ✅ Deep Links (volta para o app Flutter)
        success_url="contrax://success",
        cancel_url="contrax://cancel",
        metadata={"userId": user_id, "plano": plano},  # passa info pro webhook
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
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        print("❌ Erro no webhook Stripe:", e)
        return {"error": str(e)}

    # 🔹 Quando o pagamento for confirmado
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
    plano = data.get("plano")  # "basic" ou "standard"

    # 💵 Defina valores em R$ (ex.: 25 e 75 reais)
    prices = {
        "basic": 25.00,
        "standard": 75.00
    }

    if plano not in prices:
        return {"error": "Plano inválido"}

    # 🔹 Criar preferência (checkout MP)
    preference = {
        "items": [
            {
                "title": f"Plano {plano.capitalize()}",
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": prices[plano],
            }
        ],
        "payer": {
            "email": data.get("email", "teste@teste.com")
        },
        "metadata": {
            "userId": user_id,
            "plano": plano
        },
        # ✅ Deep Links (volta para o app Flutter)
        "back_urls": {
            "success": "contrax://success",
            "failure": "contrax://cancel"
        },
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