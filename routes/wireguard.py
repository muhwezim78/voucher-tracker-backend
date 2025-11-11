from flask import Flask, request, jsonify, g, Blueprint

wireguard_bp = Blueprint("wireguard", __name__)
