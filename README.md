<p align="center">
  <img src="icon.png" width="180" alt="Ícone do Extrator PNCP">
</p>

# Extrator de Licitações Encerradas do PNCP

Este repositório contém um script em Python, com interface gráfica (Tkinter), desenvolvido para **extrair licitações encerradas** do Portal Nacional de Contratações Públicas (PNCP) e **exportar os resultados em arquivos CSV** (.csv ou .csv_br).

O sistema permite ao usuário escolher o município e o período, realizar a consulta diretamente na API pública do PNCP e salvar os dados em formato tabular para uso posterior em análises, planilhas ou integrações.

## Arquivo principal

`pncp_extrator_licitacoes_encerradas.py`
Script único que executa toda a extração e exportação.

## Requisitos

Python 3.10 ou superior.

Bibliotecas necessárias:
`requests`
`tkinter` (já incluído na instalação padrão do Python)
`pandas`
`urllib3`

## Uso

Execute:

```
python pncp_extrator_licitacoes_encerradas.py
```

A interface gráfica será aberta, permitindo selecionar ano, município e destino dos arquivos CSV.
