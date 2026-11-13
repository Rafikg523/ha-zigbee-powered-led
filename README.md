# Zigbee LED Orchestrator

Planowana integracja Home Assistant dla sterowników LED Zigbee zasilanych przez fizyczne przekaźniki, z wykorzystaniem MQTT i natywnych grup Zigbee2MQTT.

**Status: wczesny szkielet projektu.** Repozytorium ma strukturę integracji HACS, ale nie zawiera jeszcze konfiguracji ani sterowania światłami. Nie należy instalować go do sterowania instalacją. Lista prac i kryteria odbioru znajdują się w [ROADMAP.md](ROADMAP.md); lokalna specyfikacja w `docs/` opisuje wymagania.

Docelowo repozytorium będzie można dodać w HACS jako własne repozytorium typu Integration. Samo przygotowanie struktury nie oznacza opublikowania w katalogu HACS ani Home Assistant Marketplace. Instrukcja instalacji i konfiguracji pojawi się, gdy integracja będzie gotowa do testów.

Przed aktywnym wdrożeniem wymagane są inwentaryzacja, kopia zapasowa, plan rollbacku i wyłączenie konfliktujących ścieżek sterowania.

Szczegółowy [plan dla agentów](docs/superpowers/plans/2026-11-13-zigbee-led-orchestrator.md) dzieli implementację na zadania P00–P18. Ich status i wyniki weryfikacji zapisujemy w [rejestrze postępu](docs/superpowers/plans/PROGRESS.md).
