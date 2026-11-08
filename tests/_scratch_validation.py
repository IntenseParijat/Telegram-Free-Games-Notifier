import sys,types,html,re
from typing import Any
requests=types.ModuleType("requests"); requests.Session=object; requests.RequestException=Exception; requests.utils=types.SimpleNamespace(); sys.modules["requests"]=requests
telethon=types.ModuleType("telethon"); telethon.TelegramClient=object; sys.modules["telethon"]=telethon
errors=types.ModuleType("telethon.errors"); errors.FloodWaitError=type("FloodWaitError",(Exception,),{}); sys.modules["telethon.errors"]=errors
sessions=types.ModuleType("telethon.sessions"); sessions.StringSession=object; sys.modules["telethon.sessions"]=sessions
sys.path.insert(0,"."); import notifier
actual=notifier.format_footer_line("Powered By [Intense Network](t.me/IntenseNetwork)")
expected="Powered By <a href=\"https://t.me/IntenseNetwork\">Intense Network</a>"
assert actual == expected, ("configured footer",actual,expected)
actual=notifier.format_footer_line("Powered By [Intense Network](https://t.me/IntenseNetwork)")
expected="Powered By <a href=\"https://t.me/IntenseNetwork\">Intense Network</a>"
assert actual == expected, ("full url footer",actual,expected)
actual=notifier.format_footer_line("Join [Network](t.me/IntenseNetwork) or [News](https://t.me/News) now")
expected="Join <a href=\"https://t.me/IntenseNetwork\">Network</a> or <a href=\"https://t.me/News\">News</a> now"
assert actual == expected, ("multiple footer links",actual,expected)
actual=notifier.format_footer_line("Join[Network](t.me/IntenseNetwork)")
expected="Join<a href=\"https://t.me/IntenseNetwork\">Network</a>"
assert actual == expected, ("no injected space",actual,expected)
actual=notifier.visible_text_length("🎮")
expected=2
assert actual == expected, ("emoji utf16",actual,expected)
actual=notifier.trim_description_to_fit("A &amp; B. A &amp; B. A &amp; B.","X"*98,110)
expected="A &amp; B."
assert actual == expected, ("entities",actual,expected)
actual=notifier.trim_description_to_fit("Description sentence 1. Description sentence 2. Loooooooooong Description sentence 3. Description sentence 4. Description sentence 5. Description sentence 6.","X"*98,200)
expected="Description sentence 1. Description sentence 2. Loooooooooong Description sentence 3."
assert actual == expected, ("sentence trim",actual,expected)
print("isolated caption/footer regressions: 7 passed")
