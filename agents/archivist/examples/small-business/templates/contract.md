---
title: "Договор №{{NUMBER}} от {{DATE}}"
description: "{{SHORT_DESCRIPTION}}"
created: "{{DATE}}"
updated: "{{DATE}}"
type: contract
document_subtype: contract
tags: []
status: active
source_file: "raw/{{DATE}}_{{SLUG}}.pdf"
doc_date: "{{DATE}}"
number: "{{NUMBER}}"
validity_until: "{{END_DATE}}"
amount: 0.00
currency: "RUB"
entities:
  legal_entity: [[{{LE_SLUG}}]]
  counterparty: [[{{CP_SLUG}}]]
  outlet: [[{{OUTLET_SLUG}}]]  # опционально
---

# Договор №{{NUMBER}} от {{DATE}}

## Основная информация

- **Номер:** {{NUMBER}}
- **Дата заключения:** {{DATE}}
- **Срок действия:** до {{END_DATE}}
- **Статус:** active
- **Сумма:** 0.00 RUB
- **Файл:** [[../../raw/{{DATE}}_{{SLUG}}|raw/{{DATE}}_{{SLUG}}.pdf]]

## Стороны

| Роль | Сторона | ИНН |
|------|---------|-----|
| {{LE_ROLE}} | [[{{LE_SLUG}}\|{{LE_NAME}}]] | 0000000000 |
| {{CP_ROLE}} | [[{{CP_SLUG}}\|{{CP_NAME}}]] | 0000000000 |

## Предмет договора

{{SUBJECT_DESCRIPTION}}

## Условия оплаты

- **Порядок:** {{PAYMENT_TYPE}}  <!-- предоплата / постоплата / рассрочка -->
- **Срок оплаты:** {{PAYMENT_DEADLINE}}
- **Отсрочка:** {{DEFERRED_DAYS}} дней
- **Кредитный лимит:** 0.00 RUB

## Штрафные санкции

- **Просрочка оплаты:** {{PENALTY_DESCRIPTION}}
- **Нарушение условий:** {{BREACH_PENALTY}}

## Расторжение

- **Одностороннее:** {{TERMINATION_TERMS}}
- **Споры:** {{DISPUTE_RESOLUTION}}

## Связанные документы

<!-- Ссылки на доп. соглашения, акты, счета по этому договору -->

- [[0000-00-00_addendum_example]] — доп. соглашение
- [[0000-00-00_act_example]] — акт сверки

## Заметки

_История изменений условий, особенности исполнения._
