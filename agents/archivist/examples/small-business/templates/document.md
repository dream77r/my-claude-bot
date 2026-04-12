---
title: "{{DOC_SUBTYPE}} №{{NUMBER}} от {{DATE}}"
description: "{{SHORT_DESCRIPTION}}"
created: "{{DATE}}"
updated: "{{DATE}}"
type: document
document_subtype: "{{SUBTYPE}}"  # act | invoice | waybill | payment-order | addendum
tags: []
status: active
source_file: "raw/{{DATE}}_{{SUBTYPE}}_{{SLUG}}.pdf"
doc_date: "{{DATE}}"
number: "{{NUMBER}}"
amount: 0.00
currency: "RUB"
entities:
  counterparty: [[{{CP_SLUG}}]]
  legal_entity: [[{{LE_SLUG}}]]
parent_contract: [[{{CONTRACT_SLUG}}]]
---

# {{DOC_SUBTYPE_HUMAN}} №{{NUMBER}} от {{DATE}}

## Основная информация

- **Тип:** {{DOC_SUBTYPE_HUMAN}}
- **Номер:** {{NUMBER}}
- **Дата:** {{DATE}}
- **Сумма:** 0.00 RUB
- **Файл:** [[../../raw/{{DATE}}_{{SUBTYPE}}_{{SLUG}}|raw/...]]

## Связанный договор

- [[{{CONTRACT_SLUG}}]] — {{CONTRACT_DESCRIPTION}}

## Стороны

| Сторона | ИНН |
|---------|-----|
| [[{{LE_SLUG}}\|{{LE_NAME}}]] | 0000000000 |
| [[{{CP_SLUG}}\|{{CP_NAME}}]] | 0000000000 |

## Содержание

<!-- Для акта сверки: период, итоговое сальдо.
     Для счёта: позиции, сумма, НДС.
     Для накладной: список товаров, количество, цены.
     Для платёжки: плательщик, получатель, назначение, сумма.
     Для доп. соглашения: что именно изменяется в договоре. -->

## Заметки

_Опциональные наблюдения._
