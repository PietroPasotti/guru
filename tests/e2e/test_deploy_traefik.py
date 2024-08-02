import sh

from guru import Guru, ModelState, App, Integration

traefik = App.from_charmhub("traefik-k8s", alias="traefik", channel="edge")
loki = App.from_charmhub("loki-k8s", alias="loki", channel="edge")


def test_deploy_traefik_loki():
    ms = ModelState(
        apps=[
            traefik.with_scale(1).with_config({}),
            loki.with_scale(2)
        ],
        integrations=[
            Integration.from_endpoints(traefik, 'ingress', loki, 'ingress')
        ]
    )

    g = Guru(model_state=ms)

    g.wait(
        traefik.is_active() and loki.is_active(),
        error_condition=traefik.is_blocked() or loki.is_blocked()
    )

