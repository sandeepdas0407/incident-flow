import pytest

from app.github_ops import FileEdit
from app.playbooks import guardrails as g
from app.playbooks.ui_change import recolor
from app.playbooks.vm_resize import resize_tfvars


def test_check_path_allows_scope():
    assert g.check_path("app/static/style.css", ("app/",)) == "app/static/style.css"


@pytest.mark.parametrize("path", ["infra/main.tf", "../secrets", "app/../infra/main.tf", ".github/workflows/x.yml"])
def test_check_path_rejects_out_of_scope(path):
    with pytest.raises(g.GuardrailViolation):
        g.check_path(path, ("app/",))


def test_apply_str_edit_requires_unique_match():
    assert g.apply_str_edit("a b c", "b", "X", "f") == "a X c"
    with pytest.raises(g.GuardrailViolation):
        g.apply_str_edit("b b", "b", "X", "f")
    with pytest.raises(g.GuardrailViolation):
        g.apply_str_edit("abc", "zzz", "X", "f")


def test_apply_str_edit_append():
    assert g.apply_str_edit("x\n", "", "y\n", "f") == "x\ny\n"


def test_diff_budget():
    before = "\n".join(str(i) for i in range(100))
    small = before.replace("5\n", "five\n", 1)
    assert g.check_diff_budget({"f": before}, [FileEdit("f", small)]) == 2
    big = "\n".join(f"x{i}" for i in range(100))
    with pytest.raises(g.GuardrailViolation):
        g.check_diff_budget({"f": before}, [FileEdit("f", big)])
    with pytest.raises(g.GuardrailViolation):
        g.check_diff_budget({"f": before}, [FileEdit("f", before)])


def test_vm_size_allowlist():
    assert g.check_vm_size("Standard_B2s") == "Standard_B2s"
    with pytest.raises(g.GuardrailViolation):
        g.check_vm_size("Standard_M416ms_v2")


def test_color_validation():
    assert g.check_color("#0b5fff") == "#0B5FFF"
    for bad in ["blue", "#12345", "#GGGGGG", "", "red; background:url(x)"]:
        with pytest.raises(g.GuardrailViolation):
            g.check_color(bad)


def test_resize_tfvars():
    current, out = resize_tfvars('vm_size = "Standard_B1s"\n', "Standard_B2s")
    assert current == "Standard_B1s" and out == 'vm_size = "Standard_B2s"\n'
    with pytest.raises(g.GuardrailViolation):
        resize_tfvars('vm_size = "Standard_B2s"\n', "Standard_B2s")


def test_recolor():
    css = ":root {\n  --app-background: #F4F1EA;\n}\n"
    current, out = recolor(css, "#0B5FFF")
    assert current == "#F4F1EA" and "--app-background: #0B5FFF;" in out


# ---------------- infrastructure checks ----------------
MAIN_TF = '''resource "azurerm_resource_group" "rg" {
  name = "x"
}

resource "azurerm_network_security_group" "nsg" {
  security_rule {
    name                       = "http"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    destination_port_range     = "80"
    source_address_prefix      = "*"
  }
}

resource "azurerm_linux_virtual_machine" "vm" {
  size = var.vm_size
  tags = {}
}
'''

RULE = '''
  security_rule {{
    name                       = "extra"
    priority                   = 120
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    destination_port_range     = "8443"
    source_address_prefix      = "{src}"
  }}
}}

resource "azurerm_linux_virtual_machine"'''


def _infra(new_main, tfvars='vm_size = "Standard_B1s"\n'):
    originals = {"infra/main.tf": MAIN_TF, "infra/terraform.tfvars": 'vm_size = "Standard_B1s"\n'}
    g.check_infra(originals, [FileEdit("infra/main.tf", new_main), FileEdit("infra/terraform.tfvars", tfvars)])


def test_infra_allows_tags_and_specific_cidr_rule():
    _infra(MAIN_TF.replace("tags = {}", 'tags = { owner = "ops" }'))
    _infra(MAIN_TF.replace('}\n\nresource "azurerm_linux_virtual_machine"', RULE.format(src="203.0.113.0/24"), 1))


@pytest.mark.parametrize("src", ["*", "Internet", "0.0.0.0/0"])
def test_infra_blocks_public_port(src):
    with pytest.raises(g.GuardrailViolation, match="internet"):
        _infra(MAIN_TF.replace('}\n\nresource "azurerm_linux_virtual_machine"', RULE.format(src=src), 1))


def test_infra_blocks_removed_or_renamed_resource():
    with pytest.raises(g.GuardrailViolation, match="delete or replace"):
        _infra(MAIN_TF.replace('"azurerm_linux_virtual_machine" "vm"', '"azurerm_linux_virtual_machine" "vm2"'))


def test_infra_blocks_new_paid_resource_but_allows_free_one():
    with pytest.raises(g.GuardrailViolation, match="paid"):
        _infra(MAIN_TF + '\nresource "azurerm_managed_disk" "data" {\n  disk_size_gb = 32\n}\n')
    _infra(MAIN_TF + '\nresource "azurerm_application_security_group" "web" {\n  name = "web"\n}\n')


def test_infra_blocks_count_and_bad_vm_size():
    with pytest.raises(g.GuardrailViolation, match="count"):
        _infra(MAIN_TF.replace("size = var.vm_size", "count = 2\n  size = var.vm_size"))
    with pytest.raises(g.GuardrailViolation, match="allowlist"):
        _infra(MAIN_TF, tfvars='vm_size = "Standard_D64s_v5"\n')


def test_diff_budget_custom_limit():
    before = "\n".join(str(i) for i in range(300))
    after = "\n".join(f"x{i}" if i < 80 else str(i) for i in range(300))
    with pytest.raises(g.GuardrailViolation):
        g.check_diff_budget({"f": before}, [FileEdit("f", after)])
    assert g.check_diff_budget({"f": before}, [FileEdit("f", after)], g.MAX_CHANGE_LINES) == 160
