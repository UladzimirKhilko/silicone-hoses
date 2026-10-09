from django import forms

from .models import DrawingFile, Product, ProductAlias


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ["code", "yapib", "customer", "status", "kind", "color", "angle", "d1", "d2", "l1", "l2", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        localized_fields = ["d1", "d2", "l1", "l2"]  # можно вводить «63,5»
        help_texts = {
            "code": "Точно как в штампе чертежа, например «BSI 90/60» или «BSI 90-D38L150/140-1».",
            "yapib": "Например 24.1155. Пусто — если чертежа ещё нет.",
        }


class AliasForm(forms.ModelForm):
    class Meta:
        model = ProductAlias
        fields = ["text", "source"]
        labels = {"text": "Как изделие названо в документе"}


class DrawingUploadForm(forms.ModelForm):
    class Meta:
        model = DrawingFile
        fields = ["kind", "file"]

    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size > 30 * 1024 * 1024:
            raise forms.ValidationError("Файл больше 30 МБ.")
        return f
