"""Administración de cuentas desde la terminal. Se ejecuta dentro del contenedor y no deja archivos en el equipo.

Uso (en la Thor, con terminal interactiva; no se guarda ningún script):
    docker exec -it thor-vision python -m app.auth.admin add Diego.Reyes
    docker exec -it thor-vision python -m app.auth.admin list
    docker exec -it thor-vision python -m app.auth.admin disable Osvaldo
    docker exec -it thor-vision python -m app.auth.admin enable Osvaldo
    docker exec -it thor-vision python -m app.auth.admin rol Diego.Reyes admin|operador
"""

import getpass
import re
import sys

from app.auth import passwords, store

USUARIO = re.compile(r"[A-Za-z0-9._-]{3,40}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("uso: add <usuario> | list | disable <usuario> | enable <usuario> | rol <usuario> admin|operador")
        return 1
    accion = argv[1]
    if accion == "list":
        for u in store.list_users():
            print(f"{u['username']}  rol={u['role']}  activa={bool(u['active'])}  autenticador={bool(u['totp_enabled'])}")
        return 0
    if len(argv) < 3 or not USUARIO.fullmatch(argv[2]):
        print("usuario inválido (3-40: letras, números, . _ -)")
        return 1
    usuario = argv[2]
    if accion == "add":
        if store.get_user(usuario):
            print("ese usuario ya existe")
            return 1
        clave = getpass.getpass("Contraseña (mínimo 12): ")
        if len(clave) < 12 or clave != getpass.getpass("Repite la contraseña: "):
            print("contraseña inválida o no coincide. No se guardó nada.")
            return 1
        store.create_user(usuario, passwords.hash_password(clave), "operador")
        del clave
        print("cuenta creada. En su primer inicio de sesión registrará el autenticador con el QR.")
        return 0
    cuenta = store.get_user(usuario)
    if accion in ("disable", "enable", "rol") and cuenta is None:
        print("no existe")
        return 1
    if accion == "disable":
        if cuenta["role"] == "admin" and store.admins_activos_sin(usuario) == 0:
            print("no se desactiva: es el último administrador activo")
            return 1
        store.set_active(usuario, False)
        print(f"{usuario}: desactivada")
        return 0
    if accion == "enable":
        store.set_active(usuario, True)
        print(f"{usuario}: activada")
        return 0
    if accion == "rol":
        rol = argv[3] if len(argv) > 3 else ""
        if rol not in store.ROLES:
            print("rol inválido: admin u operador")
            return 1
        if cuenta["role"] == "admin" and rol != "admin" and store.admins_activos_sin(usuario) == 0:
            print("no se quita: es el último administrador activo")
            return 1
        store.set_role(usuario, rol)
        store.log("terminal", "rol_cambiado", f"terminal -> {usuario}: {rol}")
        print(f"{usuario}: rol {rol}")
        return 0
    print("acción desconocida")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
