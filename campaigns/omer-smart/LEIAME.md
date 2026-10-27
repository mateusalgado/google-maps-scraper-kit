# Omer Smart: lista B2B de compradores (Grande Florianópolis)

Gera uma lista ranqueada de possíveis compradores das máquinas de remoção de tatuagem
a partir de 315 buscas no Google Maps (9 cidades × 6 grupos de termos).

## Rodar (no seu computador, com o Docker Desktop aberto)
```bash
cd google-maps-scraper-kit
python3 campaigns/omer-smart/omer_leads.py            # tudo
python3 campaigns/omer-smart/omer_leads.py --extras   # + termos extras (despigmentação, Q-switched, melasma…)
```
O script sobe o motor sozinho, roda uma cidade por vez e guarda cada cidade em `out/raw/`.
Se o Google limitar o IP no meio, espere um pouco e rode de novo: ele continua de onde parou.
Para reprocessar sem buscar de novo: `--rebuild`.

## Resultado (`out/`)
- `omer-leads-prioritarios.csv`: perfil comprador com telefone ou e-mail, do melhor para o pior.
- `omer-leads-todos.csv`: tudo o que foi achado na região.

Colunas: prioridade (A/B/C), nota 0–100, segmento, nome, categoria, cidade, telefone,
link de WhatsApp (celulares), e-mail, site, Instagram, Facebook, endereço, avaliação, nº de avaliações, link do Maps.

## Como a nota funciona
Base por segmento + reputação + facilidade de contato:
- 60 Já faz remoção / laser (conhece o valor, pode querer segunda máquina ou upgrade)
- 50 Dermatologia / biomedicina / harmonização
- 40 Micropigmentação / sobrancelha, Clínica de estética
- 35 Equipamentos / locação / revenda (canal: compra para alugar ou revender), Cursos / escolas
- 30 Estúdio de tatuagem
- +até 20 pelo nº de avaliações, +3 nota ≥ 4,5, +5 telefone, +5 WhatsApp, +5 e-mail, +3 site, +2 Instagram

A ≥ 70, B ≥ 50, C abaixo disso. Dentistas ("clareamento dental") e "corte a laser" ficam fora do perfil.

## Cuidados
Telefones e e-mails de profissionais são dados pessoais (LGPD): contato B2B pontual e
relevante, com opção de sair da lista. Nada de disparo em massa.
