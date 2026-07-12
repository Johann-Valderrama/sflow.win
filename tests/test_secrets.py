"""Tests de la unidad 2.3 (cobertura minima nueva): core/secrets.py.

Cubre el contrato publico de encrypt()/decrypt() (cifrado DPAPI per-user/maquina):
  - roundtrip encrypt->decrypt con string normal, unicode/acentos/emoji y string vacio
  - decrypt() nunca lanza: base64 corrupto o base64 valido pero no-DPAPI -> None
  - encrypt() produce un blob realmente distinto del texto plano (no es solo
    un passthrough de base64), documentando el comportamiento observado.

Estos tests corren en Windows real (CryptProtectData/CryptUnprotectData del
usuario actual, sin interaccion) — este repo solo corre en Windows.
"""

import base64

import pytest

from core import secrets


class TestRoundtrip:
    def test_roundtrip_normal_string(self):
        plaintext = "hello world"
        enc = secrets.encrypt(plaintext)
        assert secrets.decrypt(enc) == plaintext

    def test_roundtrip_unicode_accents_and_emoji(self):
        plaintext = "áéíóú ñ 日本語 emoji 🎉🚀"
        enc = secrets.encrypt(plaintext)
        assert secrets.decrypt(enc) == plaintext

    def test_roundtrip_empty_string(self):
        """El contrato no prohibe cadena vacia: encrypt('') cifra un blob
        de 0 bytes y decrypt() lo recupera como '' (comportamiento verificado)."""
        enc = secrets.encrypt("")
        assert secrets.decrypt(enc) == ""

    def test_roundtrip_long_string(self):
        plaintext = "x" * 5000
        enc = secrets.encrypt(plaintext)
        assert secrets.decrypt(enc) == plaintext

    def test_encrypted_output_is_base64_and_differs_from_plaintext(self):
        """encrypt() no es un passthrough: el blob DPAPI en base64 es distinto
        del texto plano codificado, y es decodificable como base64 valido."""
        plaintext = "gsk_some_api_key_1234567890"
        enc = secrets.encrypt(plaintext)
        assert enc != plaintext
        assert enc != base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
        # No lanza: es base64 valido.
        base64.b64decode(enc)

    def test_two_encryptions_of_same_plaintext_can_differ(self):
        """DPAPI puede variar el blob de salida entre llamadas (IV/salt interno);
        lo unico que debe sostenerse es que ambas descifran al mismo texto."""
        plaintext = "same secret value"
        enc_a = secrets.encrypt(plaintext)
        enc_b = secrets.encrypt(plaintext)
        assert secrets.decrypt(enc_a) == plaintext
        assert secrets.decrypt(enc_b) == plaintext


class TestDecryptNeverRaises:
    def test_decrypt_corrupt_base64_returns_none(self):
        """Base64 invalido (padding incorrecto) -> None, sin excepcion."""
        assert secrets.decrypt("not-valid-base64!!!") is None

    def test_decrypt_valid_base64_but_not_dpapi_returns_none(self):
        """Base64 bien formado pero cuyo contenido no es un blob DPAPI real
        (p. ej. de otra maquina/usuario o dato corrupto) -> None."""
        garbage = base64.b64encode(b"random junk data 1234").decode("ascii")
        assert secrets.decrypt(garbage) is None

    def test_decrypt_empty_string_returns_none(self):
        """Base64 vacio decodifica a 0 bytes, que CryptUnprotectData rechaza
        como blob invalido -> None (no un roundtrip valido de encrypt(''))."""
        assert secrets.decrypt("") is None

    @pytest.mark.parametrize(
        "bad_input",
        [
            "!!!not-base64-at-all###",
            "====",
            "a",
        ],
    )
    def test_decrypt_various_malformed_inputs_return_none(self, bad_input):
        assert secrets.decrypt(bad_input) is None
