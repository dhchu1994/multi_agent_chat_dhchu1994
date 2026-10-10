"""Interaction Portal v2 entry point alias"""
from main_v2 import create_gradio_interface

if __name__ == "__main__":
    demo = create_gradio_interface()
    demo.launch(server_name="0.0.0.0", server_port=7860, share=False)
