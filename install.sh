apt update
apt install -y libsndfile1 libclang-rt-15-dev portaudio19-dev python3-dev libspdlog-dev ffmpeg
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ 
pip install onnxruntime-1.19.2+das.opt1.dtk25041-cp310-cp310-manylinux_2_28_x86_64.whl
