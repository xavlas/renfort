FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PHOTOS_DIR=/photos DATA_DIR=/data MODELS_DIR=/app/models

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Modèles de détection (YuNet) et de reconnaissance (SFace) des visages, vérifiés par empreinte
ADD --checksum=sha256:8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4 \
    https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
    /app/models/face_detection_yunet_2023mar.onnx
ADD --checksum=sha256:0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79 \
    https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx \
    /app/models/face_recognition_sface_2021dec.onnx

COPY app /app/app

EXPOSE 2342
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:2342/api/auth')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "2342", "--proxy-headers"]
