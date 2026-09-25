# Для агента

Это локальное зеркало документации Ozon, Wildberries, Яндекс Маркета и Uzum Market.

**Правило первое: искать здесь, а не в интернете.** `docs.ozon.ru` и `dev.wildberries.ru`
закрыты антиботом — запрос оттуда вернёт капчу или 403, а тут ответ мгновенный и офлайн.

```bash
bin/mpdocs search "v3/posting/fbs/list"   # найти метод
bin/mpdocs status                          # свежесть зеркала
git -C mirror pull                         # обновиться
```

Полная инструкция — [`skill/SKILL.md`](skill/SKILL.md): что где лежит, чем `changelog`
отличается от `changes`, когда сверяться со `spec.json`. Если вы Claude Code —
запустите `./install.sh`, скилл встанет сам и будет подхватываться в каждой сессии.
