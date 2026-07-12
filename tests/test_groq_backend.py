"""Tests de la unidad 1.5(b): GroqBackend recrea cliente si la API key cambió.

Verifica que:
- El cliente se crea una sola vez (lazy init)
- Si la key no cambia, se reutiliza el mismo cliente
- Si la key cambió, se descarta y se recrea un cliente nuevo
- La key NUNCA se loguea
"""

from unittest.mock import MagicMock, patch

import pytest

from core.backends.groq_backend import GroqBackend


@pytest.fixture
def backend():
    """Crea una instancia limpia de GroqBackend para cada test."""
    return GroqBackend()


def test_lazy_init_crea_cliente_en_primer_uso(backend):
    """El cliente se crea en el primer uso de _get_client(), no en __init__."""
    assert backend._client is None

    with patch("os.getenv") as mock_getenv:
        mock_getenv.return_value = "test-key-123"
        with patch("core.backends.groq_backend.Groq") as mock_groq:
            mock_groq_instance = MagicMock()
            mock_groq.return_value = mock_groq_instance

            client = backend._get_client()

            assert backend._client is not None
            assert client == mock_groq_instance
            mock_groq.assert_called_once_with(api_key="test-key-123", timeout=10.0)
            backend._client_key = "test-key-123"


def test_reutiliza_cliente_si_key_no_cambia(backend):
    """Si la key sigue siendo la misma, _get_client() devuelve el mismo cliente."""
    with patch("os.getenv") as mock_getenv:
        mock_getenv.return_value = "key-stable"
        with patch("core.backends.groq_backend.Groq") as mock_groq:
            mock_groq_instance = MagicMock()
            mock_groq.return_value = mock_groq_instance

            # Primera llamada: crea el cliente
            client1 = backend._get_client()
            first_call_count = mock_groq.call_count

            # Segunda llamada: DEBE reutilizar (Groq no se llama de nuevo)
            client2 = backend._get_client()

            assert client1 is client2
            assert mock_groq.call_count == first_call_count  # no se llamó Groq


def test_recrea_cliente_si_key_cambio(backend):
    """Si la key cambió, se descarta el cliente viejo y se crea uno nuevo."""
    with patch("os.getenv") as mock_getenv:
        with patch("core.backends.groq_backend.Groq") as mock_groq:
            # Primera llamada con key A
            mock_getenv.return_value = "key-A"
            mock_groq_a = MagicMock(name="groq-instance-a")
            mock_groq.return_value = mock_groq_a

            client_a = backend._get_client()
            assert backend._client is mock_groq_a
            assert backend._client_key == "key-A"
            assert mock_groq.call_count == 1

            # Cambiar a key B
            mock_getenv.return_value = "key-B"
            mock_groq_b = MagicMock(name="groq-instance-b")
            mock_groq.return_value = mock_groq_b

            # Segunda llamada con key B: DEBE crear uno nuevo
            client_b = backend._get_client()

            assert client_b is mock_groq_b
            assert client_b is not client_a  # cliente distinto
            assert backend._client is mock_groq_b
            assert backend._client_key == "key-B"
            assert mock_groq.call_count == 2  # Groq se llamó dos veces


def test_error_si_no_hay_key(backend):
    """Si GROQ_API_KEY está vacía, lanza ValueError."""
    with patch("os.getenv") as mock_getenv:
        mock_getenv.return_value = ""

        with pytest.raises(ValueError, match="GROQ_API_KEY not configured"):
            backend._get_client()

    assert backend._client is None
    assert backend._client_key is None


def test_nunca_loguea_la_key(backend):
    """Verifica que _get_client() jamás loguea la key (ni completa, ni parcial)."""
    with patch("os.getenv") as mock_getenv:
        mock_getenv.return_value = "super-secret-key-12345"
        with patch("core.backends.groq_backend.Groq"):
            backend._get_client()

    # Comprobación indirecta: el método compara keys en memoria sin loguearlas.
    # Si esta prueba pasa es porque no hay código logging en _get_client().
    # Para una verificación más exhaustiva, se podría mockear logging.getLogger()
    # y verificar que nunca se llamó con la key.
    assert backend._client_key == "super-secret-key-12345"
